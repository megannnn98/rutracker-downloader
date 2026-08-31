"""Тесты сетевого слоя: детект challenge, cp1251, ретраи, валидация .torrent."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path

import httpx
import pytest

from rutracker_downloader.client import (
    MAX_RETRIES,
    MAX_RETRY_AFTER,
    RutrackerClient,
    _newest_cookie_file,
    decode,
    encode_query,
    is_challenge,
    is_torrent_payload,
    load_cookie_jar,
    looks_like_guest_page,
    parse_retry_after,
)
from rutracker_downloader.config import Settings
from rutracker_downloader.errors import (
    CloudflareChallenge,
    ConfigError,
    HttpError,
    SessionExpired,
    TorrentUnavailable,
)
from tests.conftest import fixture_text

CHALLENGE_HTML = fixture_text("cloudflare_challenge.html")
GUEST_HTML = fixture_text("guest_page.html")
TORRENT_BYTES = b"d8:announce30:http://bt.rutracker.org/annce4:infod4:name6:solarisee"
RESULTS_HTML = (
    '<html><body><table id="tor-tbl"><tr data-topic_id="1"></tr></table></body></html>'
)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        base_url="https://rutracker.net",
        user_agent="Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0",
        cookies_file=tmp_path / "cookies.txt",
    )


def make_client(settings: Settings, handler: object) -> RutrackerClient:
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    return RutrackerClient(
        settings, delay=0.0, client=httpx.Client(transport=transport)
    )


def test_challenge_detected_by_header() -> None:
    response = httpx.Response(403, headers={"cf-mitigated": "challenge"}, text="")
    assert is_challenge(response) is True


def test_challenge_detected_by_body() -> None:
    response = httpx.Response(403, text=CHALLENGE_HTML)
    assert is_challenge(response) is True


def test_plain_403_is_not_a_challenge() -> None:
    """Честный 403 форума не должен маскироваться под Cloudflare."""
    response = httpx.Response(403, text="<html><body>Доступ закрыт</body></html>")
    assert is_challenge(response) is False


def test_success_is_not_a_challenge() -> None:
    assert is_challenge(httpx.Response(200, text=RESULTS_HTML)) is False


def test_challenge_is_not_retried(settings: Settings) -> None:
    """Ретраить challenge бессмысленно: нужен свежий cf_clearance из браузера."""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            403, headers={"cf-mitigated": "challenge"}, text=CHALLENGE_HTML
        )

    with make_client(settings, handler) as client, pytest.raises(CloudflareChallenge):
        client.fetch_page("https://rutracker.net/forum/tracker.php")

    assert calls == 1


def test_guest_page_means_expired_session(settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=GUEST_HTML.encode("cp1251"))

    with make_client(settings, handler) as client, pytest.raises(SessionExpired):
        client.fetch_page("https://rutracker.net/forum/tracker.php")


def test_results_page_passes(settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=RESULTS_HTML.encode("cp1251"))

    with make_client(settings, handler) as client:
        assert "tor-tbl" in client.fetch_page("https://rutracker.net/forum/tracker.php")


def test_rate_limit_is_retried(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("rutracker_downloader.client.time.sleep", lambda _: None)
    responses = [
        httpx.Response(429, headers={"retry-after": "1"}, text="slow down"),
        httpx.Response(200, content=RESULTS_HTML.encode("cp1251")),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    with make_client(settings, handler) as client:
        assert "tor-tbl" in client.fetch_page("https://rutracker.net/forum/tracker.php")


def test_persistent_server_error_raises(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("rutracker_downloader.client.time.sleep", lambda _: None)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="maintenance")

    with make_client(settings, handler) as client, pytest.raises(HttpError):
        client.fetch_page("https://rutracker.net/forum/tracker.php")


def test_torrent_is_returned_with_referer(settings: Settings) -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["referer"] = request.headers.get("referer", "")
        return httpx.Response(
            200,
            headers={"content-type": "application/x-bittorrent"},
            content=TORRENT_BYTES,
        )

    with make_client(settings, handler) as client:
        assert client.download_torrent(42) == TORRENT_BYTES

    assert seen["referer"] == "https://rutracker.net/forum/viewtopic.php?t=42"


def test_html_instead_of_torrent_is_rejected(settings: Settings) -> None:
    """dl.php при протухшей сессии отдаёт HTML со статусом 200."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            content=GUEST_HTML.encode("cp1251"),
        )

    with make_client(settings, handler) as client, pytest.raises(SessionExpired):
        client.download_torrent(42)


def test_non_torrent_payload_is_rejected(settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "text/html"}, content=b"<html>oops"
        )

    with make_client(settings, handler) as client, pytest.raises(TorrentUnavailable):
        client.download_torrent(42)


def test_query_is_encoded_in_cp1251() -> None:
    """UTF-8 в nm= ломает русский поиск: сайт работает в cp1251."""
    assert (
        encode_query({"nm": "станислав лем"})
        == "nm=%F1%F2%E0%ED%E8%F1%EB%E0%E2+%EB%E5%EC"
    )


def test_body_is_decoded_from_cp1251() -> None:
    response = httpx.Response(200, content="Станислав Лем".encode("cp1251"))
    assert decode(response) == "Станислав Лем"


def test_guest_page_detection() -> None:
    assert looks_like_guest_page(GUEST_HTML) is True
    assert looks_like_guest_page(RESULTS_HTML) is False


# --- Retry-After -----------------------------------------------------------


def test_retry_after_delta_seconds() -> None:
    assert parse_retry_after("7") == 7.0


def test_retry_after_is_capped() -> None:
    """Сервер не должен уводить утилиту в многочасовой сон."""
    assert parse_retry_after("100000") == MAX_RETRY_AFTER


def test_retry_after_negative_is_clamped_to_zero() -> None:
    assert parse_retry_after("-5") == 0.0


def test_retry_after_http_date_in_the_future() -> None:
    moment = datetime.now(tz=UTC) + timedelta(seconds=30)
    delay = parse_retry_after(format_datetime(moment, usegmt=True))

    assert delay is not None
    assert 25.0 <= delay <= 31.0


def test_retry_after_http_date_in_the_past_is_zero() -> None:
    moment = datetime.now(tz=UTC) - timedelta(hours=1)

    assert parse_retry_after(format_datetime(moment, usegmt=True)) == 0.0


def test_retry_after_garbage_is_ignored() -> None:
    assert parse_retry_after("завтра") is None


def test_retry_after_header_is_honoured(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Пауза берётся из заголовка, а не из экспоненциального backoff."""
    slept: list[float] = []
    monkeypatch.setattr("rutracker_downloader.client.time.sleep", slept.append)
    responses = [
        httpx.Response(429, headers={"retry-after": "9"}, text="slow down"),
        httpx.Response(200, content=RESULTS_HTML.encode("cp1251")),
    ]

    with make_client(settings, lambda request: responses.pop(0)) as client:
        client.fetch_page("https://rutracker.net/forum/tracker.php")

    assert 9.0 in slept


def test_retry_attempts_are_limited(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ровно MAX_RETRIES попыток: без счётчика тест прошёл бы и при отсутствии ретраев."""
    monkeypatch.setattr("rutracker_downloader.client.time.sleep", lambda _: None)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, text="maintenance")

    with make_client(settings, handler) as client, pytest.raises(HttpError):
        client.fetch_page("https://rutracker.net/forum/tracker.php")

    assert calls == MAX_RETRIES


# --- валидация .torrent ----------------------------------------------------


def test_content_type_with_parameters_is_accepted(settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/x-bittorrent; charset=binary"},
            content=TORRENT_BYTES,
        )

    with make_client(settings, handler) as client:
        assert client.download_torrent(42) == TORRENT_BYTES


def test_bencode_garbage_is_rejected(settings: Settings) -> None:
    """Первого байта `d` мало: под него подходит любой мусор."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/x-bittorrent"},
            content=b"dnot-a-torrent",
        )

    with make_client(settings, handler) as client, pytest.raises(TorrentUnavailable):
        client.download_torrent(42)


def test_truncated_torrent_is_rejected(settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/x-bittorrent"},
            content=b"d8:announce30:http://bt.rutracker.org/annce",
        )

    with make_client(settings, handler) as client, pytest.raises(TorrentUnavailable):
        client.download_torrent(42)


def test_torrent_body_under_text_html_is_rejected(settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "text/html"}, content=TORRENT_BYTES
        )

    with make_client(settings, handler) as client, pytest.raises(TorrentUnavailable):
        client.download_torrent(42)


def test_is_torrent_payload() -> None:
    assert is_torrent_payload(TORRENT_BYTES) is True
    assert is_torrent_payload(b"dgarbage") is False
    assert is_torrent_payload(b"<html>") is False


# --- guest-детект ----------------------------------------------------------


def test_word_vhod_alone_is_not_an_expired_session() -> None:
    """«Вход» встречается в обычном тексте и не может быть признаком гостя."""
    assert (
        looks_like_guest_page("<html>Вход в пещеру. Входящие сообщения</html>") is False
    )


# --- cookie jar ------------------------------------------------------------


def test_newest_cookie_file_prefers_fresh_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "cache.txt"
    user = tmp_path / "cookies.txt"
    cache.write_text("# Netscape HTTP Cookie File\n")
    user.write_text("# Netscape HTTP Cookie File\n")
    os.utime(cache, (1000, 1000))
    os.utime(user, (2000, 2000))
    monkeypatch.setattr("rutracker_downloader.client._cache_cookie_file", lambda: cache)

    assert _newest_cookie_file(user) == user


def test_newest_cookie_file_prefers_updated_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Обновлённый сервером bb_session не должен теряться между запусками."""
    cache = tmp_path / "cache.txt"
    user = tmp_path / "cookies.txt"
    cache.write_text("# Netscape HTTP Cookie File\n")
    user.write_text("# Netscape HTTP Cookie File\n")
    os.utime(user, (1000, 1000))
    os.utime(cache, (2000, 2000))
    monkeypatch.setattr("rutracker_downloader.client._cache_cookie_file", lambda: cache)

    assert _newest_cookie_file(user) == cache


def test_missing_cookies_raise_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "rutracker_downloader.client._cache_cookie_file", lambda: tmp_path / "nope.txt"
    )

    with pytest.raises(ConfigError):
        load_cookie_jar(tmp_path / "cookies.txt")


def test_corrupt_cookie_file_raises_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken = tmp_path / "cookies.txt"
    broken.write_text("не netscape-формат\n")
    monkeypatch.setattr(
        "rutracker_downloader.client._cache_cookie_file", lambda: tmp_path / "nope.txt"
    )

    with pytest.raises(ConfigError):
        load_cookie_jar(broken)
