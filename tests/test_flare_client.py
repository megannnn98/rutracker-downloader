from __future__ import annotations

import asyncio
import gzip
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from rutracker_downloader.cli import build_parser, main
from rutracker_downloader.config import Settings
from rutracker_downloader.errors import (
    CloudflareChallenge,
    ConfigError,
    HttpError,
    SessionExpired,
)

pytest.importorskip("curl_cffi")
from rutracker_downloader.flare_client import CurlTransport, FlareClient

HTML = '<html><table id="tor-tbl"></table></html>'
TORRENT = b"d4:infod4:name4:kantee"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    cookies = tmp_path / "cookies.txt"
    cookies.write_text(
        "# Netscape HTTP Cookie File\n"
        ".rutracker.net\tTRUE\t/forum/\tTRUE\t0\tbb_session\tlogin-secret\n"
        ".rutracker.net\tTRUE\t/\tTRUE\t0\tcf_clearance\told-secret\n"
        ".example.org\tTRUE\t/\tTRUE\t0\tunrelated\tprivate\n"
    )
    return Settings("https://rutracker.net", "Firefox", cookies)


def solution() -> dict[str, object]:
    return {
        "status": "ok",
        "solution": {
            "status": 200,
            "response": HTML,
            "userAgent": "Chrome",
            "cookies": [
                {
                    "name": "bb_session",
                    "value": "new-login",
                    "domain": ".rutracker.net",
                    "path": "/forum/",
                },
                {
                    "name": "cf_clearance",
                    "value": "new-clearance",
                    "domain": ".rutracker.net",
                    "path": "/",
                },
            ],
        },
    }


def solver_stub(monkeypatch: pytest.MonkeyPatch, payload: object) -> AsyncMock:
    post = AsyncMock(
        return_value=httpx.Response(
            200, json=payload, request=httpx.Request("POST", "http://127.0.0.1:8191/v1")
        )
    )
    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    return post


def make_client(settings: Settings) -> FlareClient:
    return FlareClient(
        settings, query="кант", solver_url="http://127.0.0.1:8191", delay=0
    )


async def test_bootstrap_then_pages_and_binary_download(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = settings.cookies_file.read_bytes()
    post = solver_stub(monkeypatch, solution())
    client = make_client(settings)
    transport = client._client._transport
    request = AsyncMock(
        side_effect=[
            SimpleNamespace(
                status_code=200,
                headers=httpx.Headers({"content-encoding": "gzip"}),
                content=HTML.encode(),
            ),
            SimpleNamespace(
                status_code=200,
                headers=httpx.Headers({"content-type": "application/x-bittorrent"}),
                content=TORRENT,
            ),
        ]
    )
    monkeypatch.setattr(transport._session, "request", request)  # type: ignore[attr-defined]
    async with client:
        assert await client.fetch_page(client.search_url("кант")) == HTML
        request.assert_not_awaited()
        assert await client.fetch_page(client.search_url("кант", 50)) == HTML
        assert await client.download_torrent(42) == TORRENT
    assert client._client.is_closed
    assert settings.cookies_file.read_bytes() == before
    sent = post.call_args.kwargs["json"]["cookies"]
    assert [c["name"] for c in sent] == ["bb_session"]
    headers = dict(request.call_args.kwargs["headers"])
    assert headers["user-agent"] == "Chrome"
    assert "bb_session=new-login" in headers["cookie"]
    assert "cf_clearance=new-clearance" in headers["cookie"]
    assert headers["referer"].endswith("viewtopic.php?t=42")
    assert set(headers) == {"cookie", "referer", "user-agent"}
    assert request.call_args.kwargs["timeout"] == 30
    assert not request.call_args.kwargs["allow_redirects"]
    assert not client._seed_cookies
    assert all(c.secure for c in client._client.cookies.jar)


@pytest.mark.parametrize(
    "payload, error",
    [
        ({"status": "error", "message": "secret must not leak"}, CloudflareChallenge),
        ({"status": "ok", "solution": {"status": 403}}, CloudflareChallenge),
        ({"status": "ok", "solution": {"status": 200}}, HttpError),
        ([], CloudflareChallenge),
    ],
)
async def test_bootstrap_failure_closes_transport(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    payload: object,
    error: type[Exception],
) -> None:
    solver_stub(monkeypatch, payload)
    client = make_client(settings)
    with pytest.raises(error) as caught:
        async with client:
            pytest.fail("must not enter")
    assert "secret" not in str(caught.value)
    assert client._client.is_closed


async def test_expired_forum_session(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = solution()
    data["solution"] = {
        "status": 200,
        "response": '<input name="login_username">',
        "userAgent": "Chrome",
        "cookies": [],
    }
    solver_stub(monkeypatch, data)
    with pytest.raises(SessionExpired):
        async with make_client(settings):
            pytest.fail("must not enter")


@pytest.mark.parametrize(
    "error", [httpx.ConnectError("offline"), asyncio.CancelledError()]
)
async def test_unavailable_or_cancelled_bootstrap_closes_client(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> None:
    monkeypatch.setattr(httpx.AsyncClient, "post", AsyncMock(side_effect=error))
    client = make_client(settings)
    expected = (
        HttpError if isinstance(error, httpx.HTTPError) else asyncio.CancelledError
    )
    with pytest.raises(expected):
        async with client:
            pytest.fail("must not enter")
    assert client._client.is_closed


@pytest.mark.parametrize(
    "url",
    [
        "https://example.org",
        "http://localhost.evil",
        "http://user:secret@127.0.0.1",
        "http://127.0.0.1/v1",
    ],
)
def test_solver_must_be_local(settings: Settings, url: str) -> None:
    with pytest.raises(ConfigError):
        FlareClient(settings, query="кант", solver_url=url, delay=0)


async def test_external_redirect_is_refused(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    solver_stub(monkeypatch, solution())
    async with make_client(settings) as client:
        with pytest.raises(httpx.RequestError, match="Cross-origin"):
            await client._client.get("https://example.org/tracker.php")


def test_cli_flag_and_conflicts() -> None:
    assert (
        build_parser().parse_args(["--query", "кант", "--flaresolverr"]).flaresolverr
        == "http://127.0.0.1:8191"
    )
    with pytest.raises(SystemExit):
        main(["--query", "кант", "--flaresolverr", "--login"])


@pytest.mark.parametrize(
    "extra",
    [["--from-html", "page.html"], ["--import-downloads", "downloads"], ["--login"]],
)
def test_offline_conflicts(extra: list[str]) -> None:
    with pytest.raises(SystemExit):
        main(["--flaresolverr", *extra])


@pytest.mark.parametrize("url", ["", "   "])
def test_empty_solver_url(url: str) -> None:
    with pytest.raises(SystemExit):
        main(["--query", "кант", "--flaresolverr", url])


async def test_profile_warning(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    data = solution()
    assert isinstance(data["solution"], dict)
    data["solution"]["userAgent"] = "Mozilla/5.0 (X11; Linux x86_64) Chrome/148.0.0.0"
    solver_stub(monkeypatch, data)
    async with make_client(settings):
        pass
    assert "chrome146/macOS" in caplog.text


async def test_curl_errors_are_sanitized(monkeypatch: pytest.MonkeyPatch) -> None:
    from curl_cffi import CurlError
    from curl_cffi.requests import RequestsError

    transport = CurlTransport("https://rutracker.net", concurrency=3)
    assert transport._session.max_clients == 3
    async with httpx.AsyncClient(transport=transport) as client:
        for error in [CurlError("secret"), RequestsError("secret")]:
            monkeypatch.setattr(
                transport._session, "request", AsyncMock(side_effect=error)
            )
            with pytest.raises(httpx.RequestError) as caught:
                await client.get("https://rutracker.net")
            assert "secret" not in str(caught.value)
        with pytest.raises(httpx.RequestError, match="GET only"):
            await client.post("https://rutracker.net")


async def test_curl_real_local_http_path(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, str]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            seen.append({k.lower(): v for k, v in self.headers.items()})
            body = (
                b"invalid compressed body"
                if self.path == "/bad"
                else gzip.compress(b"decoded content")
            )
            self.send_response(200)
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Set-Cookie", "rotated=new-value; Path=/")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        monkeypatch.setenv(name, "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        async with httpx.AsyncClient(
            transport=CurlTransport(url), trust_env=False
        ) as client:
            response = await client.get(url)
            assert response.content == b"decoded content"
            assert "content-encoding" not in response.headers
            assert response.headers["content-length"] == str(len(response.content))
            await client.get(url)
            assert "rotated=new-value" in seen[1]["cookie"]
            assert "text/html" in seen[0]["accept"]
            assert "accept-language" in seen[0]
            assert seen[0]["user-agent"].startswith("python-httpx/")
            with pytest.raises(httpx.RequestError):
                await client.get(url + "/bad")
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


async def test_close_error_is_not_filesystem_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from curl_cffi import CurlError

    transport = CurlTransport("https://rutracker.net")
    await transport.aclose()
    monkeypatch.setattr(
        transport._session, "close", AsyncMock(side_effect=CurlError("secret"))
    )
    with pytest.raises(HttpError) as caught:
        await transport.aclose()
    assert "secret" not in str(caught.value)


async def test_late_challenge_has_flare_guidance(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    solver_stub(monkeypatch, solution())
    async with make_client(settings) as client:
        request = AsyncMock(
            return_value=SimpleNamespace(
                status_code=403,
                headers=httpx.Headers({"cf-mitigated": "challenge"}),
                content=b"challenge",
            )
        )
        monkeypatch.setattr(client._client._transport._session, "request", request)  # type: ignore[attr-defined]
        with pytest.raises(CloudflareChallenge, match="Сессия FlareSolverr"):
            await client.download_torrent(42)
        assert request.await_count == 1
