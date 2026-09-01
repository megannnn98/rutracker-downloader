"""HTTP-слой: единственный модуль, знающий про сеть и cookies.

Ключевая особенность RuTracker на момент написания: весь /forum/ закрыт
Cloudflare Managed Challenge (проверено: 403 + заголовок
`cf-mitigated: challenge` на login.php, tracker.php, viewtopic.php, dl.php).
Пройти его без исполнения JS нельзя, поэтому сессия строится на cookies,
экспортированных пользователем из браузера.
"""

from __future__ import annotations

import asyncio
import http.cookiejar
import logging
import random
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from types import TracebackType
from typing import Final, Self
from urllib.parse import urlencode

import httpx

from rutracker_downloader.config import Settings
from rutracker_downloader.errors import (
    CaptchaRequired,
    CloudflareChallenge,
    ConfigError,
    HttpError,
    LoginError,
    SessionExpired,
    TorrentUnavailable,
)

logger = logging.getLogger(__name__)

SITE_ENCODING: Final = "cp1251"
MAX_RETRIES: Final = 3
RETRY_STATUSES: Final = frozenset({429, 500, 502, 503, 504})
CHALLENGE_MARKER: Final = b"Just a moment"
BENCODE_PREFIX: Final = b"d"
INFO_DICT_MARKER: Final = b"4:info"
TORRENT_CONTENT_TYPES: Final = frozenset(
    {"application/x-bittorrent", "application/octet-stream"}
)
MAX_RETRY_AFTER: Final = 60.0
DEFAULT_DELAY: Final = 0.01  # суммарная частота = 1/delay; при 1.0 параллелизм не работал, при 0.01 ограничитель — --concurrency

CHALLENGE_HELP: Final = (
    "Cloudflare вернул JS-challenge. Обновите cookies: откройте rutracker в том же "
    "браузере, пройдите проверку, заново экспортируйте cookies.txt и убедитесь, что "
    "RUTRACKER_USER_AGENT совпадает с User-Agent этого браузера."
)


def _cache_cookie_file() -> Path:
    return Path.home() / ".cache" / "rutracker-downloader" / "cookies.txt"


def _newest_cookie_file(user_file: Path) -> Path | None:
    """Выбрать более свежий из пользовательского файла и рабочего кэша.

    Кэш пишется после каждого прогона и содержит cookies, обновлённые сервером
    (в первую очередь bb_session). Брать всегда пользовательский файл нельзя:
    тогда обновления сервера теряются между запусками. Свежий экспорт из
    браузера побеждает кэш по mtime — именно этого пользователь и ждёт,
    переэкспортируя cookies после протухшего cf_clearance.
    """
    candidates = [path for path in (user_file, _cache_cookie_file()) if path.exists()]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def load_cookie_jar(user_file: Path) -> http.cookiejar.MozillaCookieJar:
    """Загрузить cookies из экспортированного пользователем файла.

    Профиль браузера (cookies.sqlite) намеренно не читается: утилите достаточно
    файла, который пользователь отдал явно.
    """
    jar = http.cookiejar.MozillaCookieJar()
    source = _newest_cookie_file(user_file)
    if source is None:
        raise ConfigError(
            f"не найден файл cookies: {user_file}. Экспортируйте cookies rutracker "
            "из браузера в формате Netscape (нужны cf_clearance и bb_session)."
        )

    try:
        jar.load(str(source), ignore_discard=True, ignore_expires=True)
    except (OSError, http.cookiejar.LoadError) as exc:
        raise ConfigError(f"не удалось прочитать {source}: {exc}") from exc
    logger.debug("loaded %d cookies from %s", len(jar), source)
    return jar


def is_torrent_payload(body: bytes) -> bool:
    """Минимальная проверка metainfo: bencode-словарь с ключом info.

    Одного первого байта `d` мало — под него подходит любой мусор, начинающийся
    с этой буквы. Полноценный bencode-парсер здесь избыточен: нам достаточно
    отсечь HTML, обрезанные и подменённые ответы.
    """
    return body.startswith(BENCODE_PREFIX) and INFO_DICT_MARKER in body


def decode(response: httpx.Response) -> str:
    """Декодировать тело в cp1251 — кодировку RuTracker."""
    return response.content.decode(SITE_ENCODING, errors="replace")


def encode_query(params: dict[str, str]) -> str:
    """Собрать query-строку в cp1251: иначе русский поиск не работает."""
    return urlencode(params, encoding=SITE_ENCODING)


def parse_retry_after(value: str) -> float | None:
    """Разобрать Retry-After: delta-seconds или HTTP-date (RFC 9110).

    Прошедшая дата даёт 0, будущая — усечена до MAX_RETRY_AFTER: сервер не
    должен уводить утилиту в многочасовой сон.
    """
    try:
        return min(max(float(value), 0.0), MAX_RETRY_AFTER)
    except ValueError:
        pass
    try:
        moment = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    delay = (moment - datetime.now(tz=UTC)).total_seconds()
    return min(max(delay, 0.0), MAX_RETRY_AFTER)


def is_challenge(response: httpx.Response) -> bool:
    """Отличить Cloudflare-challenge от честного 403 самого форума."""
    if response.status_code != httpx.codes.FORBIDDEN:
        return False
    if response.headers.get("cf-mitigated") == "challenge":
        return True
    return CHALLENGE_MARKER in response.content[:4096]


def looks_like_guest_page(html: str) -> bool:
    """Признак протухшего bb_session: форум отдал страницу гостя.

    Завязываемся только на поле формы логина. Слово «вход» сюда не годится:
    оно встречается в обычном тексте («входящие», «вход в пещеру»), и любая
    валидная страница без таблицы раздач ошибочно считалась бы протухшей.
    """
    lowered = html.lower()
    if "tor-tbl" in lowered or "logout=" in lowered:
        return False
    return "login_username" in lowered


class RutrackerClient:
    """Сессия к RuTracker: cookies, задержки, ретраи, детект challenge."""

    def __init__(
        self,
        settings: Settings,
        *,
        delay: float = DEFAULT_DELAY,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._delay = delay
        self._jar = load_cookie_jar(settings.cookies_file) if client is None else None
        self._client = client or httpx.AsyncClient(
            cookies=self._jar,
            follow_redirects=True,
            http2=False,
            timeout=httpx.Timeout(30.0),
            headers={
                "User-Agent": settings.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
                "Upgrade-Insecure-Requests": "1",
            },
        )
        self._owns_client = client is None
        self._last_request_at: float | None = None
        self._rate_limit_lock = asyncio.Lock()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    async def close(self) -> None:
        try:
            if self._owns_client:
                await self._client.aclose()
        finally:
            self._persist_cookies()

    def _persist_cookies(self) -> None:
        """Сохранить рабочий jar в кэш, не трогая файл пользователя."""
        if self._jar is None:
            return
        target = _cache_cookie_file()
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            self._jar.save(str(target), ignore_discard=True, ignore_expires=True)
            target.chmod(0o600)
        except OSError as exc:  # не повод ронять прогон
            logger.warning("не удалось сохранить cookies в %s: %s", target, exc)

    async def _throttle(self) -> None:
        """Сериализует запросы: интервал start-to-start.

        При одинаковом --delay нагрузка на сайт выросла по сравнению с
        синхронной версией (response-to-start): запросы стартуют чаще,
        но не чаще delay между стартами.
        """
        async with self._rate_limit_lock:
            if self._last_request_at is None:
                self._last_request_at = time.monotonic()
                return
            jitter = self._delay * random.uniform(0.8, 1.2)
            elapsed = time.monotonic() - self._last_request_at
            if elapsed < jitter:
                await asyncio.sleep(jitter - elapsed)
            self._last_request_at = time.monotonic()

    async def _backoff(self, pause: float) -> None:
        """Спать pause секунд под rate-limit lock.

        429/Retry-After тормозит весь клиент, а не одну корутину.
        """
        async with self._rate_limit_lock:
            await asyncio.sleep(pause)
            self._last_request_at = time.monotonic()

    def _retry_delay(self, response: httpx.Response, attempt: int) -> float:
        """Пауза перед повтором: Retry-After, иначе экспоненциальный backoff."""
        retry_after = response.headers.get("retry-after")
        if retry_after is not None:
            seconds = parse_retry_after(retry_after)
            if seconds is not None:
                return seconds
        return float(2**attempt)

    async def request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
        """Выполнить запрос с задержкой, ретраями и детектом challenge."""
        last: httpx.Response | None = None
        for attempt in range(MAX_RETRIES):
            await self._throttle()
            try:
                response = await self._client.request(method, url, **kwargs)  # type: ignore[arg-type]
            except httpx.HTTPError as exc:
                if attempt == MAX_RETRIES - 1:
                    raise HttpError(f"{method} {url}: {exc}") from exc
                await self._backoff(float(2**attempt))
                continue

            # Challenge не ретраим: свежий cf_clearance берётся только из браузера.
            if is_challenge(response):
                raise CloudflareChallenge(CHALLENGE_HELP)

            if response.status_code in RETRY_STATUSES and attempt < MAX_RETRIES - 1:
                pause = self._retry_delay(response, attempt)
                logger.warning(
                    "%s %s -> %d, повтор через %.1f с",
                    method,
                    url,
                    response.status_code,
                    pause,
                )
                await self._backoff(pause)
                last = response
                continue

            if response.status_code >= httpx.codes.BAD_REQUEST:
                raise HttpError(f"{method} {url} -> HTTP {response.status_code}")
            return response

        status = last.status_code if last is not None else "?"
        raise HttpError(
            f"{method} {url}: не удалось за {MAX_RETRIES} попыток (последний {status})"
        )

    def cookie_values(self) -> list[str]:
        """Значения текущих cookies — нужны для вычистки их из фикстур."""
        return [cookie.value for cookie in self._client.cookies.jar if cookie.value]

    def search_url(self, query: str, start: int = 0) -> str:
        params = {"nm": query} if start == 0 else {"nm": query, "start": str(start)}
        return f"{self._settings.search_url}?{encode_query(params)}"

    async def fetch_page(self, url: str) -> str:
        """Скачать страницу форума и убедиться, что мы не гость."""
        html = decode(await self.request("GET", url))
        if looks_like_guest_page(html):
            raise SessionExpired(
                "RuTracker считает нас гостем: cookie bb_session протух. "
                "Переэкспортируйте cookies.txt из браузера."
            )
        return html

    async def download_torrent(self, topic_id: int) -> bytes:
        """Скачать .torrent и проверить, что это действительно torrent-файл."""
        response = await self.request(
            "GET",
            self._settings.download_url(topic_id),
            headers={"Referer": self._settings.topic_url(topic_id)},
        )
        content_type = (
            response.headers.get("content-type", "").split(";")[0].strip().lower()
        )
        body = response.content
        if content_type not in TORRENT_CONTENT_TYPES or not is_torrent_payload(body):
            if looks_like_guest_page(decode(response)):
                raise SessionExpired(
                    f"topic {topic_id}: вместо торрента пришла страница логина "
                    "(bb_session протух)"
                )
            raise TorrentUnavailable(
                f"topic {topic_id}: ответ не является .torrent (content-type={content_type!r})"
            )
        return body

    async def login(self, username: str, password: str) -> None:
        """Логин по паролю. Fallback на случай снятия Cloudflare-challenge."""
        body = urlencode(
            {"login_username": username, "login_password": password, "login": "вход"},
            encoding=SITE_ENCODING,
        )
        response = await self.request(
            "POST",
            self._settings.login_url,
            content=body.encode("ascii"),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": self._settings.login_url,
                "Origin": self._settings.base_url,
            },
        )
        page = decode(response).lower()
        if "cap_sid" in page or "cap_code_" in page:
            raise CaptchaRequired("RuTracker запросил CAPTCHA; обход не реализуется")
        if not any(cookie.name == "bb_session" for cookie in self._client.cookies.jar):
            raise LoginError("логин не дал cookie bb_session: проверьте логин и пароль")
