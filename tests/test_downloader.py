"""Тесты оркестрации: фильтрация, идемпотентность, dry-run, остановка на challenge.

HTML здесь синтетический и намеренно минимальный: это тест логики прогона,
а не селекторов. Селекторы проверяются в test_parser.py на живых фикстурах.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rutracker_downloader.downloader import Downloader
from rutracker_downloader.errors import CloudflareChallenge, TorrentUnavailable
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

    def fetch_page(self, url: str) -> str:
        return self.pages[url]

    def download_torrent(self, topic_id: int) -> bytes:
        if topic_id in self.failing_topics:
            raise TorrentUnavailable(f"topic {topic_id}: ответ не является .torrent")
        self.downloaded.append(topic_id)
        return TORRENT_BYTES


@pytest.fixture
def pages() -> dict[str, str]:
    return {SEARCH_URL: PAGE_ONE, NEXT_PAGE_URL: PAGE_TWO}


def test_audio_is_excluded_and_ebooks_downloaded(
    pages: dict[str, str], tmp_path: Path
) -> None:
    client = FakeClient(pages)
    stats = Downloader(client, tmp_path).run("лем")

    assert client.downloaded == [1, 5]
    assert stats.audio_excluded == 1
    assert stats.downloaded == 2
    assert stats.missing_link == 1


def test_pagination_is_followed_and_duplicates_skipped(
    pages: dict[str, str], tmp_path: Path
) -> None:
    stats = Downloader(FakeClient(pages), tmp_path).run("лем")

    assert stats.pages == 2
    assert stats.duplicates == 1


def test_unknown_is_skipped_but_reported(pages: dict[str, str], tmp_path: Path) -> None:
    stats = Downloader(FakeClient(pages), tmp_path).run("лем")

    assert stats.unknown_skipped == 1
    assert stats.unknown_titles == ["Лем Станислав - Собрание сочинений"]


def test_include_unknown_downloads_it(pages: dict[str, str], tmp_path: Path) -> None:
    client = FakeClient(pages)
    Downloader(client, tmp_path, include_unknown=True).run("лем")

    assert 3 in client.downloaded


def test_dry_run_creates_no_files(pages: dict[str, str], tmp_path: Path) -> None:
    output = tmp_path / "out"
    client = FakeClient(pages)
    stats = Downloader(client, output, dry_run=True).run("лем")

    assert client.downloaded == []
    assert stats.downloaded == 0
    assert not output.exists()


def test_second_run_is_idempotent(pages: dict[str, str], tmp_path: Path) -> None:
    Downloader(FakeClient(pages), tmp_path).run("лем")
    second_client = FakeClient(pages)
    stats = Downloader(second_client, tmp_path).run("лем")

    assert second_client.downloaded == []
    assert stats.downloaded == 0
    assert stats.already_exists == 2


def test_existing_file_is_not_overwritten(
    pages: dict[str, str], tmp_path: Path
) -> None:
    Downloader(FakeClient(pages), tmp_path).run("лем")
    saved = min(tmp_path.glob("*.torrent"))
    saved.write_bytes(b"d4:test4:keep")

    Downloader(FakeClient(pages), tmp_path).run("лем")

    assert saved.read_bytes() == b"d4:test4:keep"


def test_no_part_files_left_behind(pages: dict[str, str], tmp_path: Path) -> None:
    Downloader(FakeClient(pages), tmp_path).run("лем")

    assert list(tmp_path.glob("*.part")) == []


def test_download_error_is_counted_and_run_continues(
    pages: dict[str, str], tmp_path: Path
) -> None:
    client = FakeClient(pages, failing_topics={1})
    stats = Downloader(client, tmp_path).run("лем")

    assert stats.errors == 1
    assert client.downloaded == [5]


def test_max_pages_limits_the_crawl(pages: dict[str, str], tmp_path: Path) -> None:
    stats = Downloader(FakeClient(pages), tmp_path, max_pages=1).run("лем")

    assert stats.pages == 1


def test_challenge_aborts_the_run(tmp_path: Path) -> None:
    """Cloudflare-challenge останавливает прогон: ретраить бессмысленно."""

    class ChallengingClient(FakeClient):
        def fetch_page(self, url: str) -> str:
            raise CloudflareChallenge("Cloudflare вернул JS-challenge")

    with pytest.raises(CloudflareChallenge):
        Downloader(ChallengingClient({}), tmp_path).run("лем")


def test_existing_target_is_not_overwritten_by_save(
    pages: dict[str, str], tmp_path: Path
) -> None:
    """Гонка двух процессов: победитель один, проигравший не затирает результат."""
    downloader = Downloader(FakeClient(pages), tmp_path)
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


def test_save_leaves_no_temporary_files(pages: dict[str, str], tmp_path: Path) -> None:
    downloader = Downloader(FakeClient(pages), tmp_path)
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


def test_lost_race_counts_as_already_exists(
    pages: dict[str, str], tmp_path: Path
) -> None:
    Downloader(FakeClient(pages), tmp_path).run("лем")
    for path in tmp_path.glob("*.torrent"):
        path.write_bytes(b"d4:spam4:eggse")

    client = FakeClient(pages)
    downloader = Downloader(client, tmp_path)
    stats = downloader.run("лем")

    assert stats.downloaded == 0
    assert stats.already_exists == 2
