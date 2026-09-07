"""Optional FlareSolverr bootstrap with a curl_cffi HTTP transport."""

from __future__ import annotations

import http.cookiejar
import logging
import re
from typing import Self
from urllib.parse import urlsplit

import httpx

from rutracker_downloader.client import (
    RutrackerClient,
    looks_like_guest_page,
)
from rutracker_downloader.config import Settings
from rutracker_downloader.errors import (
    CloudflareChallenge,
    ConfigError,
    HttpError,
    SessionExpired,
)

logger = logging.getLogger(__name__)
FORWARDED_HEADERS = frozenset({"cookie", "referer", "user-agent"})
REQUEST_TIMEOUT = 30


class CurlTransport(httpx.AsyncBaseTransport):
    """Keep HTTPX cookie/redirect handling and the existing retry policy."""

    def __init__(self, base_url: str, *, concurrency: int = 20) -> None:
        try:
            from curl_cffi import CurlOpt
            from curl_cffi.requests import AsyncSession
        except ImportError as exc:
            raise ConfigError(
                "Для --flaresolverr выполните uv sync --extra flaresolverr"
            ) from exc
        self._origin = httpx.URL(base_url)
        self._session = AsyncSession(
            impersonate="chrome146",
            discard_cookies=True,
            max_clients=concurrency,
            curl_options={CurlOpt.PROXY: ""},
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        from curl_cffi import CurlError

        if request.method != "GET":
            raise httpx.RequestError(
                "FlareSolverr mode supports GET only", request=request
            )
        if (request.url.scheme, request.url.host, request.url.port) != (
            self._origin.scheme,
            self._origin.host,
            self._origin.port,
        ):
            raise httpx.RequestError("Cross-origin request refused", request=request)
        try:
            response = await self._session.request(
                "GET",
                str(request.url),
                headers=[
                    (k, v)
                    for k, v in request.headers.multi_items()
                    if k.lower() in FORWARDED_HEADERS
                ],
                data=await request.aread(),
                allow_redirects=False,
                timeout=REQUEST_TIMEOUT,
            )
        except CurlError as exc:
            # Exceptions from transports may contain request details; log no cookies.
            raise httpx.RequestError(
                f"curl_cffi: {type(exc).__name__}", request=request
            ) from exc
        # libcurl already decoded the body; decoding failures raise CurlError.
        headers = [
            (k, v)
            for k, v in response.headers.multi_items()
            if v is not None and k.lower() not in {"content-encoding", "content-length"}
        ]
        return httpx.Response(
            response.status_code, headers=headers, content=response.content
        )

    async def aclose(self) -> None:
        from curl_cffi import CurlError

        try:
            await self._session.close()
        except CurlError as exc:
            raise HttpError("curl_cffi: не удалось закрыть транспорт") from exc


class FlareClient(RutrackerClient):
    """Obtain fresh clearance once per run; never overwrite Firefox cookies."""

    def __init__(
        self,
        settings: Settings,
        *,
        query: str,
        solver_url: str,
        delay: float,
        concurrency: int = 20,
    ) -> None:
        endpoint = urlsplit(solver_url)
        if (
            endpoint.scheme not in {"http", "https"}
            or endpoint.hostname not in {"localhost", "127.0.0.1", "::1"}
            or endpoint.username
            or endpoint.password
            or endpoint.query
            or endpoint.fragment
            or endpoint.path not in {"", "/"}
        ):
            raise ConfigError(
                "FlareSolverr URL должен указывать на локальный сервис без пути: http://127.0.0.1:8191"
            )
        origin = urlsplit(settings.base_url)
        if (
            origin.scheme != "https"
            or not origin.hostname
            or origin.username
            or origin.password
        ):
            raise ConfigError(
                "Для --flaresolverr нужен HTTPS --base-url без учётных данных"
            )
        jar = http.cookiejar.MozillaCookieJar()
        try:
            jar.load(
                str(settings.cookies_file), ignore_discard=True, ignore_expires=True
            )
        except (OSError, http.cookiejar.LoadError) as exc:
            raise ConfigError(
                "Не удалось загрузить cookies для FlareSolverr; выполните экспорт cookies"
            ) from exc
        # Scope the exported cookies before sending them to the local browser.
        self._seed_cookies = [
            {
                "name": c.name,
                "value": c.value,
                "domain": c.domain,
                "path": c.path,
                "secure": c.secure,
            }
            for c in jar
            if c.name != "cf_clearance" and c.domain.lstrip(".") == origin.hostname
        ]
        client = httpx.AsyncClient(
            transport=CurlTransport(settings.base_url, concurrency=concurrency),
            follow_redirects=True,
            timeout=None,
        )
        super().__init__(settings, delay=delay, client=client)
        self._owns_client = True
        self._solver_url = solver_url.rstrip("/") + "/v1"
        self._initial_url = self.search_url(query)
        self._initial_html: str | None = None

    async def __aenter__(self) -> Self:
        try:
            await self._bootstrap()
        except BaseException:
            await self.close()
            raise
        return self

    async def _bootstrap(self) -> None:
        logger.info("FlareSolverr: получение сессии (таймаут 60 с)")
        try:
            async with httpx.AsyncClient(timeout=75, trust_env=False) as solver:
                response = await solver.post(
                    self._solver_url,
                    json={
                        "cmd": "request.get",
                        "url": self._initial_url,
                        "maxTimeout": 60000,
                        "cookies": self._seed_cookies,
                    },
                )
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPError as exc:
            raise HttpError(
                "FlareSolverr недоступен: проверьте docker compose up -d"
            ) from exc
        except ValueError as exc:
            raise HttpError("FlareSolverr вернул некорректный JSON") from exc
        if not isinstance(data, dict) or data.get("status") != "ok":
            raise CloudflareChallenge(
                "FlareSolverr не получил сессию за отведённое время"
            )
        solution = data.get("solution")
        if not isinstance(solution, dict) or solution.get("status") != 200:
            raise CloudflareChallenge("FlareSolverr не получил HTTP 200 от RuTracker")
        html, ua, cookies = (
            solution.get("response"),
            solution.get("userAgent"),
            solution.get("cookies"),
        )
        if (
            not isinstance(html, str)
            or not isinstance(ua, str)
            or not ua
            or not isinstance(cookies, list)
        ):
            raise HttpError("FlareSolverr: неполный ответ сессии")
        if looks_like_guest_page(html):
            raise SessionExpired(
                "FlareSolverr: сессия RuTracker протухла; нужен свежий экспорт bb_session"
            )
        hostname = urlsplit(self._settings.base_url).hostname
        for cookie in cookies:
            if not isinstance(cookie, dict):
                raise HttpError("FlareSolverr: неверный формат cookies")
            name, value, domain, path = (
                cookie.get(k) for k in ("name", "value", "domain", "path")
            )
            if not all(isinstance(v, str) for v in (name, value, domain, path)):
                raise HttpError("FlareSolverr: неверные поля cookie")
            assert isinstance(name, str) and isinstance(value, str)
            assert isinstance(domain, str) and isinstance(path, str)
            if domain.lstrip(".") == hostname:
                secure = cookie.get("secure", True)
                if not isinstance(secure, bool):
                    raise HttpError("FlareSolverr: неверное поле secure cookie")
                self._client.cookies.jar.set_cookie(
                    http.cookiejar.Cookie(
                        version=0,
                        name=name,
                        value=value,
                        port=None,
                        port_specified=False,
                        domain=domain,
                        domain_specified=True,
                        domain_initial_dot=domain.startswith("."),
                        path=path,
                        path_specified=True,
                        secure=secure,
                        expires=None,
                        discard=True,
                        comment=None,
                        comment_url=None,
                        rest={},
                        rfc2109=False,
                    )
                )
        self._client.headers["User-Agent"] = ua
        browser = re.search(r"Chrome/(\d+)", ua)
        if "Macintosh" not in ua or browser is None or browser.group(1) != "146":
            logger.warning(
                "FlareSolverr: браузер отличается от профиля curl chrome146/macOS; "
                "совместимость сессии не гарантирована"
            )
        self._initial_html = html
        self._seed_cookies = []
        logger.info("FlareSolverr: сессия получена, дальнейшие запросы через curl_cffi")

    async def fetch_page(self, url: str) -> str:
        if url == self._initial_url and self._initial_html is not None:
            html, self._initial_html = self._initial_html, None
            return html
        return await super().fetch_page(url)

    async def request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
        try:
            return await super().request(method, url, **kwargs)
        except CloudflareChallenge as exc:
            raise CloudflareChallenge(
                "Сессия FlareSolverr больше не принимается: повторите запуск для получения новой"
            ) from exc
