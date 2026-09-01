"""Тесты CLI: валидация аргументов, коды возврата, отчёт при аварийном обрыве."""

from __future__ import annotations

from pathlib import Path
from typing import Self

import pytest

from rutracker_downloader.cli import (
    EXIT_CONFIG,
    EXIT_IO,
    EXIT_NETWORK,
    EXIT_OK,
    EXIT_PARTIAL,
    main,
)
from rutracker_downloader.config import Settings
from rutracker_downloader.downloader import Stats
from rutracker_downloader.errors import CloudflareChallenge, ConfigError

SETTINGS = Settings(
    base_url="https://rutracker.net",
    user_agent="Mozilla/5.0 (X11; Linux x86_64; rv:154.0) Gecko/20100101 Firefox/154.0",
    cookies_file=Path("cookies.txt"),
)


class StubClient:
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


def install_stubs(
    monkeypatch: pytest.MonkeyPatch,
    *,
    run_effect: Exception | None = None,
    stats: Stats | None = None,
) -> Stats:
    """Подменить настройки, клиент и загрузчик, чтобы CLI не ходил в сеть."""
    prepared = stats or Stats()

    class StubDownloader:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self.stats = prepared

        async def run(self, query: str) -> Stats:
            if run_effect is not None:
                raise run_effect
            return self.stats

    monkeypatch.setattr("rutracker_downloader.cli.Stats", lambda: prepared)
    monkeypatch.setattr(
        "rutracker_downloader.cli.load_settings", lambda **kwargs: SETTINGS
    )
    monkeypatch.setattr(
        "rutracker_downloader.cli.RutrackerClient", lambda *a, **kw: StubClient()
    )
    monkeypatch.setattr("rutracker_downloader.cli.Downloader", StubDownloader)
    return prepared


@pytest.mark.parametrize(
    "argv", [["--delay", "-1"], ["--max-pages", "0"], ["--max-pages", "-3"]]
)
def test_invalid_arguments_are_rejected(argv: list[str]) -> None:
    """--max-pages 0 раньше давал «успех» с нулевой статистикой."""
    with pytest.raises(SystemExit) as exit_info:
        main(["--query", "лем", *argv])

    assert exit_info.value.code == 2  # argparse usage error


def test_zero_delay_is_allowed() -> None:
    from rutracker_downloader.cli import build_parser

    assert build_parser().parse_args(["--query", "лем", "--delay", "0"]).delay == 0.0


def test_default_delay_and_concurrency() -> None:
    from rutracker_downloader.cli import build_parser

    args = build_parser().parse_args(["--query", "лем"])
    assert args.delay == 0.01
    assert args.concurrency == 20


def test_successful_run_returns_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    install_stubs(monkeypatch, stats=Stats(found=3, downloaded=3))

    assert main(["--query", "лем"]) == EXIT_OK


def test_download_errors_give_partial_code(monkeypatch: pytest.MonkeyPatch) -> None:
    install_stubs(monkeypatch, stats=Stats(found=3, downloaded=2, errors=1))

    assert main(["--query", "лем"]) == EXIT_PARTIAL


def test_challenge_gives_network_code_and_prints_report(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    install_stubs(
        monkeypatch,
        run_effect=CloudflareChallenge("Cloudflare вернул JS-challenge"),
        stats=Stats(found=10, downloaded=4),
    )

    code = main(["--query", "лем"])
    printed = capsys.readouterr()

    assert code == EXIT_NETWORK
    assert "скачано новых            : 4" in printed.out


def test_config_error_gives_config_code(monkeypatch: pytest.MonkeyPatch) -> None:
    def raising(**kwargs: object) -> Settings:
        raise ConfigError("не задан RUTRACKER_USER_AGENT")

    monkeypatch.setattr("rutracker_downloader.cli.load_settings", raising)

    assert main(["--query", "лем"]) == EXIT_CONFIG


def test_config_error_from_client_gives_config_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ConfigError из RutrackerClient.__init__ (load_cookie_jar) → EXIT_CONFIG."""

    class RaisingClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise ConfigError("не найден файл cookies")

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

    install_stubs(monkeypatch, stats=Stats(found=3, downloaded=3))
    monkeypatch.setattr("rutracker_downloader.cli.RutrackerClient", RaisingClient)

    assert main(["--query", "лем"]) == EXIT_CONFIG


def test_filesystem_error_is_reported_not_traced(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """OSError при mkdir/записи раньше давал traceback вместо отчёта."""
    install_stubs(
        monkeypatch,
        run_effect=PermissionError(13, "Permission denied"),
        stats=Stats(found=5, downloaded=2),
    )

    code = main(["--query", "лем"])
    printed = capsys.readouterr()

    assert code == EXIT_IO
    assert "ошибка файловой системы" in printed.err
    assert "скачано новых            : 2" in printed.out


def test_unknown_titles_are_listed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    install_stubs(
        monkeypatch,
        stats=Stats(
            found=1, unknown_skipped=1, unknown_titles=["Лем - Собрание сочинений"]
        ),
    )

    main(["--query", "лем"])

    assert "Лем - Собрание сочинений" in capsys.readouterr().out
