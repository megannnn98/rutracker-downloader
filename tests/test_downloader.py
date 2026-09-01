"""Тесты оркестрации: фильтрация, идемпотентность, dry-run, остановка на challenge.

HTML здесь синтетический и намеренно минимальный: это тест логики прогона,
а не селекторов. Селекторы проверяются в test_parser.py на живых фикстурах.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from rutracker_downloader.downloader import Downloader
from rutracker_downloader.errors import (
    CloudflareChallenge,
    SessionExpired,
    TorrentUnavailable,
)
from rutracker_downloader.models import TorrentEntry

TORRENT_BYTES = b"d8:announce30:http://bt.rutracker.org/annce"
SEARCH_URL = "https://rutracker.net/forum/tracker.php?nm=lem"
NEXT_PAGE_URL = "https://rutracker.net/forum/tracker.php?nm=lem&start=50"


def _row(topic_id: int, title: str, forum: str, *, with_link: bool = True) -> str:
    link = (
        f'<a class="tr-dl" href="dl.php?t={topic_id}">1.2 MB</a>' if with_link else ""
    )
    return (
        f'<tr id="trs-tr-{topic_id}" data-topic_id="{topic_id}">'
        f'<td><a class="f-name" href="tracker.php?f=1">{forum}</a></td>'
        f'<td><a class="tLink" href="viewtopic.php?t={topic_id}">{title}</a></td>'
        f"<td>{link}</td></tr>"
    )


PAGE_ONE = (
    '<html><body><table id="tor-tbl"><tbody>'
    + _row(1, "Лем Станислав - Солярис [FB2]", "Художественная литература")
    + _row(2, "Лем Станислав - Солярис [MP3, 128 kbps]", "Аудиокниги")
    + _row(3, "Лем Станислав - Собрание сочинений", "Художественная литература")
    + _row(4, "Лем Станислав - Фиаско [EPUB]", "Книги", with_link=False)
    + "</tbody></table>"
    '<a href="tracker.php?nm=lem&start=50">2</a>'
    "</body></html>"
)

PAGE_TWO = (
    '<html><body><table id="tor-tbl"><tbody>'
    + _row(1, "Лем Станислав - Солярис [FB2]", "Художественная литература")  # дубликат
    + _row(5, "Лем Станислав - Сумма технологии [DJVU]", "Книги")
    + "</tbody></table></body></html>"
)


THIRD_PAGE_URL = "https://rutracker.net/forum/tracker.php?nm=lem&start=100"
FOURTH_PAGE_URL = "https://rutracker.net/forum/tracker.php?nm=lem&start=150"
PAGE_THREE = (
    '<html><body><table id="tor-tbl"><tbody>'
    + _row(6, "Лем Станислав - Эдем [FB2]", "Книги")
    + "</tbody></table></body></html>"
)

_ALL_LINKS = "".join(
    f'<a href="tracker.php?nm=lem&start={n}">{n // 50 + 1}</a>' for n in (50, 100, 150)
)


def _fanout_pages() -> dict[str, str]:
    """Каждая страница линкует ВСЕ остальные — уровень BFS > 1 страницы."""
    body = (
        '<html><body><table id="tor-tbl"><tbody>{rows}</tbody></table>'
        + _ALL_LINKS
        + "</body></html>"
    )
    return {
        SEARCH_URL: body.format(rows=_row(1, "Лем - Солярис [FB2]", "Книги")),
        NEXT_PAGE_URL: body.format(
            rows=_row(5, "Лем - Сумма технологии [DJVU]", "Книги")
        ),
        THIRD_PAGE_URL: body.format(rows=_row(6, "Лем - Эдем [FB2]", "Книги")),
        FOURTH_PAGE_URL: body.format(rows=_row(7, "Лем - Непобедимый [FB2]", "Книги")),
    }


class FakeClient:
    """Подставной клиент: отдаёт заранее заданные страницы и торренты."""

    def __init__(
        self, pages: dict[str, str], *, failing_topics: set[int] | None = None
    ) -> None:
        self.pages = pages
        self.failing_topics = failing_topics or set()
        self.downloaded: list[int] = []

    def search_url(self, query: str, start: int = 0) -> str:
        return SEARCH_URL

    async def fetch_page(self, url: str) -> str:
        return self.pages[url]

    async def download_torrent(self, topic_id: int) -> bytes:
        if topic_id in self.failing_topics:
            raise TorrentUnavailable(f"topic {topic_id}: ответ не является .torrent")
        self.downloaded.append(topic_id)
        return TORRENT_BYTES


def _pages() -> dict[str, str]:
    return {SEARCH_URL: PAGE_ONE, NEXT_PAGE_URL: PAGE_TWO}


PAGE_TWO_WITH_PAGINATION = (
    '<html><body><table id="tor-tbl"><tbody>'
    + _row(1, "Лем Станислав - Солярис [FB2]", "Художественная литература")
    + _row(5, "Лем Станислав - Сумма технологии [DJVU]", "Книги")
    + "</tbody></table>"
    '<a href="tracker.php?nm=lem&start=100">3</a>'
    "</body></html>"
)


async def test_audio_is_excluded_and_ebooks_downloaded(tmp_path: Path) -> None:
    client = FakeClient(_pages())
    stats = await Downloader(client, tmp_path).run("лем")

    assert sorted(client.downloaded) == [1, 5]
    assert stats.audio_excluded == 1
    assert stats.downloaded == 2
    assert stats.missing_link == 1


async def test_pagination_is_followed_and_duplicates_skipped(tmp_path: Path) -> None:
    stats = await Downloader(FakeClient(_pages()), tmp_path).run("лем")

    assert stats.pages == 2
    assert stats.duplicates == 1


async def test_unknown_is_skipped_but_reported(tmp_path: Path) -> None:
    stats = await Downloader(FakeClient(_pages()), tmp_path).run("лем")

    assert stats.unknown_skipped == 1
    assert stats.unknown_titles == ["Лем Станислав - Собрание сочинений"]


async def test_include_unknown_downloads_it(tmp_path: Path) -> None:
    client = FakeClient(_pages())
    await Downloader(client, tmp_path, include_unknown=True).run("лем")

    assert 3 in client.downloaded


async def test_dry_run_creates_no_files(tmp_path: Path) -> None:
    output = tmp_path / "out"
    client = FakeClient(_pages())
    stats = await Downloader(client, output, dry_run=True).run("лем")

    assert client.downloaded == []
    assert stats.downloaded == 0
    assert not output.exists()


async def test_second_run_is_idempotent(tmp_path: Path) -> None:
    await Downloader(FakeClient(_pages()), tmp_path).run("лем")
    second_client = FakeClient(_pages())
    stats = await Downloader(second_client, tmp_path).run("лем")

    assert second_client.downloaded == []
    assert stats.downloaded == 0
    assert stats.already_exists == 2


async def test_existing_file_is_not_overwritten(tmp_path: Path) -> None:
    await Downloader(FakeClient(_pages()), tmp_path).run("лем")
    saved = min(tmp_path.glob("*.torrent"))
    saved.write_bytes(b"d4:test4:keep")

    await Downloader(FakeClient(_pages()), tmp_path).run("лем")

    assert saved.read_bytes() == b"d4:test4:keep"


async def test_no_part_files_left_behind(tmp_path: Path) -> None:
    await Downloader(FakeClient(_pages()), tmp_path).run("лем")

    assert list(tmp_path.glob("*.part")) == []


async def test_download_error_is_counted_and_run_continues(tmp_path: Path) -> None:
    client = FakeClient(_pages(), failing_topics={1})
    stats = await Downloader(client, tmp_path).run("лем")

    assert stats.errors == 1
    assert 5 in client.downloaded


async def test_max_pages_limits_the_crawl(tmp_path: Path) -> None:
    stats = await Downloader(FakeClient(_pages()), tmp_path, max_pages=1).run("лем")

    assert stats.pages == 1


async def test_challenge_aborts_the_run(tmp_path: Path) -> None:
    """Cloudflare-challenge останавливает прогон: ретраить бессмысленно."""

    class ChallengingClient(FakeClient):
        async def fetch_page(self, url: str) -> str:
            raise CloudflareChallenge("Cloudflare вернул JS-challenge")

    with pytest.raises(CloudflareChallenge):
        await Downloader(ChallengingClient({}), tmp_path).run("лем")


async def test_existing_target_is_not_overwritten_by_save(tmp_path: Path) -> None:
    """Гонка двух процессов: победитель один, проигравший не затирает результат."""
    downloader = Downloader(FakeClient(_pages()), tmp_path)
    entry = TorrentEntry(
        topic_id=42,
        title="Лем - Солярис [FB2]",
        forum="Книги",
        topic_url="https://rutracker.net/forum/viewtopic.php?t=42",
        download_url="https://rutracker.net/forum/dl.php?t=42",
    )
    tmp_path.mkdir(parents=True, exist_ok=True)

    assert downloader._save(entry, TORRENT_BYTES) is True
    assert downloader._save(entry, b"d4:spam4:eggse") is False

    saved = next(tmp_path.glob("*.torrent"))
    assert saved.read_bytes() == TORRENT_BYTES


async def test_save_leaves_no_temporary_files(tmp_path: Path) -> None:
    downloader = Downloader(FakeClient(_pages()), tmp_path)
    entry = TorrentEntry(
        topic_id=42,
        title="Лем - Солярис [FB2]",
        forum="Книги",
        topic_url="https://rutracker.net/forum/viewtopic.php?t=42",
        download_url="https://rutracker.net/forum/dl.php?t=42",
    )

    downloader._save(entry, TORRENT_BYTES)
    downloader._save(entry, TORRENT_BYTES)

    assert [p.name for p in tmp_path.iterdir() if p.suffix == ".part"] == []
    assert len(list(tmp_path.iterdir())) == 1


async def test_lost_race_counts_as_already_exists(tmp_path: Path) -> None:
    await Downloader(FakeClient(_pages()), tmp_path).run("лем")
    for path in tmp_path.glob("*.torrent"):
        path.write_bytes(b"d4:spam4:eggse")

    client = FakeClient(_pages())
    downloader = Downloader(client, tmp_path)
    stats = await downloader.run("лем")

    assert stats.downloaded == 0
    assert stats.already_exists == 2


async def test_crawl_error_preserves_already_collected_entries(
    tmp_path: Path,
) -> None:
    """Challenge на 4-й странице не отменяет раздачи с 1–3."""
    page_three_with_link_to_bad = (
        '<html><body><table id="tor-tbl"><tbody>'
        + _row(6, "Лем Станислав - Эдем [FB2]", "Книги")
        + "</tbody></table>"
        '<a href="tracker.php?nm=lem&start=150">4</a>'
        "</body></html>"
    )
    pages = {
        SEARCH_URL: PAGE_ONE,
        NEXT_PAGE_URL: PAGE_TWO_WITH_PAGINATION,
        THIRD_PAGE_URL: page_three_with_link_to_bad,
    }

    class PartialClient(FakeClient):
        async def fetch_page(self, url: str) -> str:
            if "start=150" in url:
                raise CloudflareChallenge("JS-challenge")
            return pages[url]

    client = PartialClient(pages)
    with pytest.raises(CloudflareChallenge):
        await Downloader(client, tmp_path).run("лем")

    assert sorted(client.downloaded) == [1, 5, 6]


async def test_no_duplicate_page_requests(tmp_path: Path) -> None:
    """visited.update(level) до gather — повторных HTTP-запросов одной страницы нет."""
    pages = _fanout_pages()

    class CountingClient(FakeClient):
        def __init__(self, pages: dict[str, str]) -> None:
            super().__init__(pages)
            self.fetch_counts: dict[str, int] = {}

        async def fetch_page(self, url: str) -> str:
            self.fetch_counts[url] = self.fetch_counts.get(url, 0) + 1
            return await super().fetch_page(url)

    client = CountingClient(pages)
    await Downloader(client, tmp_path).run("лем")

    for url, count in client.fetch_counts.items():
        assert count == 1, f"{url} запрошена {count} раз"


async def test_crawl_respects_concurrency(tmp_path: Path) -> None:
    """Обход не превышает concurrency одновременных запросов."""
    pages = _fanout_pages()

    class ConcurrencyTrackingClient(FakeClient):
        def __init__(self, pages: dict[str, str]) -> None:
            super().__init__(pages)
            self.in_flight = 0
            self.max_in_flight = 0

        async def fetch_page(self, url: str) -> str:
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            await asyncio.sleep(0.05)
            self.in_flight -= 1
            return pages[url]

    client = ConcurrencyTrackingClient(pages)
    await Downloader(client, tmp_path, concurrency=2).run("лем")

    assert client.max_in_flight == 2


async def test_download_gather_completes_all_before_return(
    tmp_path: Path,
) -> None:
    """return_exceptions=True: все задачи завершаются, первая ошибка пробрасывается."""
    pages = _pages()

    class SlowSessionExpiredClient(FakeClient):
        def __init__(self, pages: dict[str, str]) -> None:
            super().__init__(pages, failing_topics={1})
            self.completed: list[int] = []

        async def download_torrent(self, topic_id: int) -> bytes:
            if topic_id in self.failing_topics:
                await asyncio.sleep(0.01)
                raise SessionExpired("bb_session протух")
            await asyncio.sleep(0.05)
            self.completed.append(topic_id)
            return TORRENT_BYTES

    client = SlowSessionExpiredClient(pages)
    with pytest.raises(SessionExpired):
        await Downloader(client, tmp_path).run("лем")

    assert 5 in client.completed


async def test_session_expired_during_download_propagates(
    tmp_path: Path,
) -> None:
    """P1-NEW: SessionExpired при скачивании пробрасывается, остальные — докачаны."""
    pages = _pages()

    class ExpiringClient(FakeClient):
        def __init__(self, pages: dict[str, str]) -> None:
            super().__init__(pages)
            self.completed: list[int] = []

        async def download_torrent(self, topic_id: int) -> bytes:
            if topic_id == 1:
                raise SessionExpired("bb_session протух")
            self.completed.append(topic_id)
            return TORRENT_BYTES

    client = ExpiringClient(pages)
    with pytest.raises(SessionExpired):
        await Downloader(client, tmp_path).run("лем")

    assert 5 in client.completed


async def test_save_oserror_propagates(tmp_path: Path) -> None:
    """P1-NEW: OSError из _save пробрасывается наружу (EXIT_IO в CLI)."""
    pages = _pages()
    client = FakeClient(pages)

    class BrokenSaveDownloader(Downloader):
        def _save(self, entry: TorrentEntry, payload: bytes) -> bool:
            raise PermissionError(13, "Permission denied")

    with pytest.raises(PermissionError):
        await BrokenSaveDownloader(client, tmp_path).run("лем")
