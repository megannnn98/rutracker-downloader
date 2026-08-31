"""Тесты парсера на живых фикстурах RuTracker.

Фикстуры сняты скриптом scripts/dump_fixtures.py с реальной выдачи по запросу
«станислав лем» и очищены от sid/логина/значений cookies. Селекторы здесь
проверены по факту: a.f-name, встречающийся в старых парсерах, на актуальной
странице не существует — категория лежит в td.f-name-col.
"""

from __future__ import annotations

import pytest

from rutracker_downloader.models import SearchPage
from rutracker_downloader.parser import parse_search_page
from tests.conftest import fixture_text

PAGE_URL = (
    "https://rutracker.net/forum/tracker.php?nm=%F1%F2%E0%ED%E8%F1%EB%E0%E2+%EB%E5%EC"
)
ROWS_PER_PAGE = 50


@pytest.fixture
def page() -> SearchPage:
    return parse_search_page(fixture_text("search_page1.html"), PAGE_URL)


def test_all_rows_are_parsed(page: SearchPage) -> None:
    assert len(page.entries) == ROWS_PER_PAGE


def test_first_entry_fields(page: SearchPage) -> None:
    entry = page.entries[0]

    assert entry.topic_id == 6876545
    assert (
        entry.title == "Лем Станислав - Солярис [Алексей Крутиков, 2022, 192 kbps, MP3]"
    )
    assert (
        entry.forum == "[Аудио] Зарубежная фантастика, фэнтези, мистика, ужасы, фанфики"
    )
    assert entry.topic_url == "https://rutracker.net/forum/viewtopic.php?t=6876545"
    assert entry.download_url == "https://rutracker.net/forum/dl.php?t=6876545"


def test_every_entry_has_required_fields(page: SearchPage) -> None:
    for entry in page.entries:
        assert entry.topic_id > 0
        assert entry.title
        assert entry.forum
        assert entry.topic_url.startswith(
            "https://rutracker.net/forum/viewtopic.php?t="
        )


def test_topic_ids_are_unique(page: SearchPage) -> None:
    ids = [entry.topic_id for entry in page.entries]
    assert len(set(ids)) == len(ids)


def test_download_links_are_absolute(page: SearchPage) -> None:
    for entry in page.entries:
        assert entry.download_url is not None
        assert (
            entry.download_url
            == f"https://rutracker.net/forum/dl.php?t={entry.topic_id}"
        )


def test_pagination_is_taken_from_html_with_search_id(page: SearchPage) -> None:
    """URL страниц нельзя конструировать: RuTracker подмешивает search_id."""
    assert len(page.pagination_urls) == 3
    for url in page.pagination_urls:
        assert url.startswith("https://rutracker.net/forum/tracker.php?")
        assert "search_id=" in url
        assert "start=" in url


def test_second_page_parses_too() -> None:
    second = parse_search_page(fixture_text("search_page2.html"), PAGE_URL)

    assert len(second.entries) == ROWS_PER_PAGE
    assert second.entries[0].topic_id == 4732660


def test_pages_contain_different_releases(page: SearchPage) -> None:
    second = parse_search_page(fixture_text("search_page2.html"), PAGE_URL)
    first_ids = {entry.topic_id for entry in page.entries}
    second_ids = {entry.topic_id for entry in second.entries}

    assert not first_ids & second_ids


def test_row_without_download_link_yields_none() -> None:
    """Раздача без ссылки на .torrent — штатный случай, а не ошибка парсинга."""
    html = (
        '<table id="tor-tbl"><tr data-topic_id="777">'
        '<td class="f-name-col"><a href="tracker.php?f=1">Форум</a></td>'
        '<td><a class="tLink" href="viewtopic.php?t=777">Раздача</a></td>'
        "</tr></table>"
    )
    entry = parse_search_page(html, PAGE_URL).entries[0]

    assert entry.download_url is None


def test_empty_html_is_not_a_crash() -> None:
    empty = parse_search_page("<html><body>ничего не найдено</body></html>", PAGE_URL)

    assert empty.entries == ()
    assert empty.pagination_urls == ()


def test_guest_page_yields_nothing() -> None:
    assert parse_search_page(fixture_text("guest_page.html"), PAGE_URL).entries == ()


def test_foreign_origin_pagination_is_dropped() -> None:
    """Абсолютная ссылка на чужой хост не должна попасть в обход."""
    html = (
        '<a href="https://external.example/forum/tracker.php?start=50">чужой</a>'
        '<a href="tracker.php?search_id=abc&start=50">свой</a>'
    )
    page = parse_search_page(html, PAGE_URL)

    assert page.pagination_urls == (
        "https://rutracker.net/forum/tracker.php?search_id=abc&start=50",
    )


def test_same_host_other_scheme_is_dropped() -> None:
    html = '<a href="http://rutracker.net/forum/tracker.php?start=50">downgrade</a>'

    assert parse_search_page(html, PAGE_URL).pagination_urls == ()
