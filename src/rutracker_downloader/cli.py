"""CLI: разбор аргументов, коды возврата, печать статистики."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from rutracker_downloader.client import RutrackerClient
from rutracker_downloader.config import load_settings
from rutracker_downloader.downloader import DEFAULT_MAX_PAGES, Downloader, Stats
from rutracker_downloader.errors import (
    CloudflareChallenge,
    ConfigError,
    LoginError,
    RutrackerError,
    SessionExpired,
)

EXIT_OK = 0
EXIT_CONFIG = 1
EXIT_NETWORK = 2
EXIT_PARTIAL = 3
EXIT_IO = 4

DEFAULT_DELAY = 1.0


def non_negative_float(raw: str) -> float:
    """--delay: отрицательная пауза молча отключила бы троттлинг."""
    value = float(raw)
    if value < 0:
        raise argparse.ArgumentTypeError("задержка не может быть отрицательной")
    return value


def positive_int(raw: str) -> int:
    """--max-pages: 0 дал бы «успешный» прогон без единого запроса."""
    value = int(raw)
    if value < 1:
        raise argparse.ArgumentTypeError("нужно хотя бы 1")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m rutracker_downloader",
        description="Скачивает только .torrent-файлы по результатам поиска RuTracker.",
    )
    parser.add_argument("--query", required=True, help="поисковый запрос")
    parser.add_argument(
        "--output", type=Path, default=Path("./output"), help="каталог для .torrent"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="только показать подходящие раздачи"
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="подробный лог")
    parser.add_argument(
        "--delay",
        type=non_negative_float,
        default=DEFAULT_DELAY,
        help="пауза между запросами, с",
    )
    parser.add_argument(
        "--max-pages",
        type=positive_int,
        default=DEFAULT_MAX_PAGES,
        help="предел страниц выдачи",
    )
    parser.add_argument(
        "--include-unknown",
        action="store_true",
        help="качать и раздачи без книжных и аудио-маркеров",
    )
    parser.add_argument("--base-url", default=None, help="зеркало RuTracker")
    parser.add_argument(
        "--cookies", type=Path, default=None, help="Netscape-файл с cookies"
    )
    parser.add_argument(
        "--login",
        action="store_true",
        help="залогиниться по паролю (работает, только если снят Cloudflare-challenge)",
    )
    return parser


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(message)s",
        stream=sys.stderr,
    )


def print_report(stats: Stats, *, include_unknown: bool) -> None:
    print("\n" + stats.render())
    if stats.unknown_titles and not include_unknown:
        print(f"\nнеопределённые раздачи ({len(stats.unknown_titles)}), не скачаны:")
        for title in stats.unknown_titles:
            print(f"  - {title}")
        print("Чтобы скачать и их, повторите запуск с --include-unknown.")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose)

    try:
        settings = load_settings(base_url=args.base_url, cookies_file=args.cookies)
    except ConfigError as exc:
        print(f"ошибка конфигурации: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    stats = Stats()
    try:
        with RutrackerClient(settings, delay=args.delay) as client:
            if args.login:
                if not settings.username or not settings.password:
                    print(
                        "для --login задайте RUTRACKER_USERNAME и RUTRACKER_PASSWORD",
                        file=sys.stderr,
                    )
                    return EXIT_CONFIG
                client.login(settings.username, settings.password)

            downloader = Downloader(
                client,
                args.output,
                include_unknown=args.include_unknown,
                dry_run=args.dry_run,
                max_pages=args.max_pages,
            )
            stats = downloader.stats
            downloader.run(args.query)
    except (ConfigError, LoginError) as exc:
        print(f"ошибка доступа: {exc}", file=sys.stderr)
        print_report(stats, include_unknown=args.include_unknown)
        return EXIT_CONFIG
    except (CloudflareChallenge, SessionExpired) as exc:
        print(f"\nпрогон остановлен: {exc}", file=sys.stderr)
        print_report(stats, include_unknown=args.include_unknown)
        return EXIT_NETWORK
    except RutrackerError as exc:
        print(f"\nсетевая ошибка: {exc}", file=sys.stderr)
        print_report(stats, include_unknown=args.include_unknown)
        return EXIT_NETWORK
    except OSError as exc:
        # mkdir, запись временного файла, os.link: без этого был бы traceback
        # вместо отчёта, и статистика частичного прогона терялась бы.
        print(f"\nошибка файловой системы: {exc}", file=sys.stderr)
        print_report(stats, include_unknown=args.include_unknown)
        return EXIT_IO

    print_report(stats, include_unknown=args.include_unknown)
    return EXIT_PARTIAL if stats.errors else EXIT_OK
