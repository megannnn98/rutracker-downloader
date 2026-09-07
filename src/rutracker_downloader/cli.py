"""CLI: разбор аргументов, коды возврата, печать статистики."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from rutracker_downloader.client import DEFAULT_DELAY, RutrackerClient
from rutracker_downloader.config import DEFAULT_BASE_URL, Settings, load_settings
from rutracker_downloader.downloader import (
    DEFAULT_CONCURRENCY,
    DEFAULT_MAX_PAGES,
    Downloader,
    Stats,
)
from rutracker_downloader.errors import (
    CloudflareChallenge,
    ConfigError,
    LoginError,
    RutrackerError,
    SessionExpired,
)
from rutracker_downloader.models import TorrentEntry
from rutracker_downloader.offline import (
    import_downloads,
    plan_downloads,
    write_links,
)

EXIT_OK = 0
EXIT_CONFIG = 1
EXIT_NETWORK = 2
EXIT_PARTIAL = 3
EXIT_IO = 4


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
    parser.add_argument("--query", help="поисковый запрос (не нужен с --from-html)")
    parser.add_argument(
        "--from-html",
        type=Path,
        nargs="+",
        metavar="PATH",
        help="разобрать сохранённые из браузера страницы выдачи вместо запросов "
        "к сайту; каталоги разворачиваются в .html/.htm внутри них",
    )
    parser.add_argument(
        "--import-downloads",
        type=Path,
        nargs="+",
        metavar="PATH",
        help="разложить уже скачанные браузером .torrent по схеме именования "
        "проекта; вместе с --from-html имена получат ещё и названия раздач",
    )
    parser.add_argument(
        "--links-out",
        type=Path,
        default=None,
        help="куда записать ссылки на .torrent в режиме --from-html "
        "(по умолчанию <output>/links.txt)",
    )
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
        "--concurrency",
        type=positive_int,
        default=DEFAULT_CONCURRENCY,
        help="параллельных скачиваний",
    )
    parser.add_argument(
        "--include-unknown",
        action="store_true",
        help="качать и раздачи без книжных и аудио-маркеров",
    )
    parser.add_argument("--base-url", default=None, help="зеркало RuTracker")
    parser.add_argument(
        "--flaresolverr",
        nargs="?",
        const="http://127.0.0.1:8191",
        metavar="URL",
        help="получить сессию через локальный FlareSolverr и использовать curl_cffi",
    )
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
        format="%(asctime)s.%(msecs)03d %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )


def print_report(stats: Stats, *, include_unknown: bool) -> None:
    print("\n" + stats.render())
    if stats.unknown_titles and not include_unknown:
        print(f"\nнеопределённые раздачи ({len(stats.unknown_titles)}), не скачаны:")
        for title in stats.unknown_titles:
            print(f"  - {title}")
        print("Чтобы скачать и их, повторите запуск с --include-unknown.")


async def _run(settings: Settings, args: argparse.Namespace, stats: Stats) -> int:
    if args.flaresolverr:
        from rutracker_downloader.flare_client import FlareClient

        selected_client: RutrackerClient = FlareClient(
            settings, query=args.query, solver_url=args.flaresolverr, delay=args.delay
        )
    else:
        selected_client = RutrackerClient(settings, delay=args.delay)
    async with selected_client as client:
        if args.login:
            if not settings.username or not settings.password:
                print(
                    "для --login задайте RUTRACKER_USERNAME и RUTRACKER_PASSWORD",
                    file=sys.stderr,
                )
                return EXIT_CONFIG
            await client.login(settings.username, settings.password)

        downloader = Downloader(
            client,
            args.output,
            include_unknown=args.include_unknown,
            dry_run=args.dry_run,
            max_pages=args.max_pages,
            concurrency=args.concurrency,
            stats=stats,
        )
        await downloader.run(args.query)

    return EXIT_PARTIAL if stats.errors else EXIT_OK


def run_offline(args: argparse.Namespace) -> int:
    """Офлайн-ветка: сеть не нужна, поэтому не нужны ни cookies, ни User-Agent."""
    stats = Stats()
    base_url = (args.base_url or DEFAULT_BASE_URL).rstrip("/")

    planned: list[TorrentEntry] = []
    if args.from_html:
        planned = plan_downloads(
            args.from_html,
            search_url=f"{base_url}/forum/tracker.php",
            include_unknown=args.include_unknown,
            stats=stats,
        )

    if args.import_downloads:
        # Названия берём из разобранных страниц; без них имена выйдут
        # из одного topic_id, что всё равно лучше обрезанных браузерных.
        titles = {entry.topic_id: entry.title for entry in planned}
        # Браузерный сниппет качает выдачу целиком, поэтому при разборе
        # страниц импорт ограничивается тем, что прошло фильтр.
        allowed = set(titles) if args.from_html else None
        import_downloads(
            args.import_downloads, titles, args.output, stats, allowed=allowed
        )
    elif args.dry_run:
        for entry in planned:
            print(
                f"  [dry-run] {entry.topic_id}  {entry.title}  <- {entry.download_url}"
            )
    else:
        target = args.links_out or args.output / "links.txt"
        write_links(planned, target)
        print(f"\nссылок записано: {len(planned)} -> {target}")

    print_report(stats, include_unknown=args.include_unknown)
    return EXIT_PARTIAL if stats.errors else EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(args.verbose)

    if args.flaresolverr and (args.login or args.from_html or args.import_downloads):
        parser.error("--flaresolverr несовместим с --login и офлайн-режимами")

    if args.from_html or args.import_downloads:
        try:
            return run_offline(args)
        except OSError as exc:
            print(f"\nошибка файловой системы: {exc}", file=sys.stderr)
            return EXIT_IO

    if not args.query:
        parser.error(
            "нужен --query (или --from-html / --import-downloads для офлайн-режима)"
        )

    try:
        settings = load_settings(
            base_url=args.base_url,
            cookies_file=args.cookies,
            require_user_agent=not bool(args.flaresolverr),
        )
    except ConfigError as exc:
        print(f"ошибка конфигурации: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    include_unknown = args.include_unknown
    stats = Stats()

    try:
        exit_code = asyncio.run(_run(settings, args, stats))
    except (ConfigError, LoginError) as exc:
        print(f"ошибка доступа: {exc}", file=sys.stderr)
        print_report(stats, include_unknown=include_unknown)
        return EXIT_CONFIG
    except (CloudflareChallenge, SessionExpired) as exc:
        print(f"\nпрогон остановлен: {exc}", file=sys.stderr)
        print_report(stats, include_unknown=include_unknown)
        return EXIT_NETWORK
    except RutrackerError as exc:
        print(f"\nсетевая ошибка: {exc}", file=sys.stderr)
        print_report(stats, include_unknown=include_unknown)
        return EXIT_NETWORK
    except OSError as exc:
        print(f"\nошибка файловой системы: {exc}", file=sys.stderr)
        print_report(stats, include_unknown=include_unknown)
        return EXIT_IO

    print_report(stats, include_unknown=include_unknown)
    return exit_code
