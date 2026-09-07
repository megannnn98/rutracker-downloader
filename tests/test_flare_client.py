from __future__ import annotations

import asyncio
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
from rutracker_downloader.flare_client import FlareClient

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
