"""Офлайн-разбор сохранённых страниц выдачи."""

from __future__ import annotations

from pathlib import Path

import pytest

from rutracker_downloader.cli import main
from rutracker_downloader.downloader import Stats
from rutracker_downloader.offline import (
    collect_html_files,
    import_downloads,
    plan_downloads,
    read_page,
    write_links,
)
from rutracker_downloader.torrent_file import topic_id_from_torrent

FIXTURES = Path(__file__).parent / "fixtures"
SEARCH_URL = "https://rutracker.net/forum/tracker.php"


def test_collect_html_files_expands_directory_sorted() -> None:
    files = collect_html_files([FIXTURES])
    names = [path.name for path in files]
    assert names == sorted(names)
    assert "search_page1.html" in names


def test_collect_html_files_keeps_explicit_file() -> None:
    target = FIXTURES / "search_page1.html"
    assert collect_html_files([target]) == [target]


def test_read_page_decodes_cp1251(tmp_path: Path) -> None:
    """cp1251-байты не образуют валидный UTF-8 — на этом и строится определение."""
    target = tmp_path / "page.html"
    target.write_bytes("<html>Кант</html>".encode("cp1251"))
    assert "Кант" in read_page(target)


def test_read_page_decodes_utf8(tmp_path: Path) -> None:
    target = tmp_path / "page.html"
    target.write_bytes("<html>Кант</html>".encode())
    assert "Кант" in read_page(target)


def test_plan_downloads_uses_same_filtering_as_online() -> None:
    stats = Stats()
    planned = plan_downloads(
        [FIXTURES / "search_page1.html"],
        search_url=SEARCH_URL,
        include_unknown=False,
        stats=stats,
    )

    assert stats.pages == 1
    assert stats.found > 0
    assert stats.audio_excluded > 0
    assert planned
    assert all(entry.download_url is not None for entry in planned)
    assert all(
        entry.download_url is not None
        and entry.download_url.startswith("https://rutracker.net/forum/dl.php?t=")
        for entry in planned
    )


def test_plan_downloads_deduplicates_across_pages() -> None:
    """Одна и та же страница дважды даёт дубликаты, а не удвоенный план."""
    page = FIXTURES / "search_page1.html"
    single = Stats()
    plan_downloads([page], search_url=SEARCH_URL, include_unknown=False, stats=single)

    doubled = Stats()
    planned = plan_downloads(
        [page, page], search_url=SEARCH_URL, include_unknown=False, stats=doubled
    )

    assert doubled.pages == 2
    assert doubled.duplicates == single.found
    assert len(planned) == len(
        plan_downloads(
            [page], search_url=SEARCH_URL, include_unknown=False, stats=Stats()
        )
    )


def test_plan_downloads_warns_on_page_without_entries(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Сохранённая страница проверки Cloudflare не должна пройти незамеченной."""
    with caplog.at_level("WARNING"):
        plan_downloads(
            [FIXTURES / "cloudflare_challenge.html"],
            search_url=SEARCH_URL,
            include_unknown=False,
            stats=Stats(),
        )
    assert "ни одной раздачи" in caplog.text


def test_write_links_one_per_line(tmp_path: Path) -> None:
    stats = Stats()
    planned = plan_downloads(
        [FIXTURES / "search_page1.html"],
        search_url=SEARCH_URL,
        include_unknown=False,
        stats=stats,
    )
    target = tmp_path / "nested" / "links.txt"
    write_links(planned, target)

    lines = target.read_text(encoding="utf-8").splitlines()
    assert len(lines) == len(planned)
    assert all(line.startswith("https://") for line in lines)


def test_cli_from_html_writes_links_without_network(tmp_path: Path) -> None:
    """Ключевое: офлайн-ветка не требует ни cookies, ни RUTRACKER_USER_AGENT."""
    output = tmp_path / "out"
    code = main(
        [
            "--from-html",
            str(FIXTURES / "search_page1.html"),
            "--output",
            str(output),
        ]
    )
    assert code == 0
    assert (output / "links.txt").read_text(encoding="utf-8").strip()


def test_cli_requires_query_without_from_html() -> None:
    with pytest.raises(SystemExit):
        main(["--output", "/tmp/does-not-matter"])


def make_torrent(topic_id: int | None, name: bytes = b"kniga.fb2") -> bytes:
    """Минимальный bencode-словарь: comment со ссылкой на тему плюс info.

    После comment намеренно идёт ещё один ключ: именно на этой границе
    наивный поиск по сырым байтам склеивал бы цифры id с длиной следующей
    строки и возвращал бы неверный topic_id.
    """

    def string(raw: bytes) -> bytes:
        return str(len(raw)).encode() + b":" + raw

    parts = b""
    if topic_id is not None:
        url = f"https://rutracker.net/forum/viewtopic.php?t={topic_id}".encode()
        parts += string(b"comment") + string(url)
    parts += string(b"created by") + string(b"uTorrent/3.6")
    parts += string(b"info") + b"d" + string(b"name") + string(name) + b"e"
    return b"d" + parts + b"e"


def test_topic_id_from_torrent_reads_comment() -> None:
    assert topic_id_from_torrent(make_torrent(6886527)) == 6886527


def test_topic_id_from_torrent_none_without_link() -> None:
    assert topic_id_from_torrent(make_torrent(None)) is None


def test_import_downloads_names_by_topic_id_and_title(tmp_path: Path) -> None:
    source = tmp_path / "downloads"
    source.mkdir()
    # Имя файла намеренно бессмысленное: браузер обрезает его и теряет id.
    (source / "obrezannoe [rutracker-.torrent").write_bytes(make_torrent(6886527))
    output = tmp_path / "out"
    stats = Stats()

    import_downloads(
        [source], {6886527: "Крюков А. Н. - Немецкая философия"}, output, stats
    )

    assert stats.downloaded == 1
    assert [p.name for p in output.iterdir()] == [
        "6886527_Крюков_А._Н._-_Немецкая_философия.torrent"
    ]


def test_import_downloads_falls_back_to_bare_topic_id(tmp_path: Path) -> None:
    source = tmp_path / "downloads"
    source.mkdir()
    (source / "whatever.torrent").write_bytes(make_torrent(123))
    output = tmp_path / "out"
    stats = Stats()

    import_downloads([source], {}, output, stats)

    assert [p.name for p in output.iterdir()] == ["123.torrent"]


def test_import_downloads_skips_zero_byte_and_counts_error(tmp_path: Path) -> None:
    """Оборвавшиеся загрузки дают файлы нулевого размера — их шесть в ~/Downloads."""
    source = tmp_path / "downloads"
    source.mkdir()
    (source / "broken.torrent").write_bytes(b"")
    (source / "good.torrent").write_bytes(make_torrent(555))
    output = tmp_path / "out"
    stats = Stats()

    import_downloads([source], {}, output, stats)

    assert stats.errors == 1
    assert stats.downloaded == 1
    assert [p.name for p in output.iterdir()] == ["555.torrent"]


def test_import_downloads_rejects_bencode_without_info_dict(tmp_path: Path) -> None:
    """Файл разбирается и содержит ссылку на тему, но торрентом не является.

    Отдельно от нулевого файла: там отказ случился бы и без проверки
    содержимого, а здесь единственный барьер — is_torrent_payload.
    """
    source = tmp_path / "downloads"
    source.mkdir()
    url = b"https://rutracker.net/forum/viewtopic.php?t=999"
    (source / "fake.torrent").write_bytes(
        b"d7:comment" + str(len(url)).encode() + b":" + url + b"e"
    )
    output = tmp_path / "out"
    stats = Stats()

    import_downloads([source], {}, output, stats)

    assert stats.errors == 1
    assert stats.downloaded == 0
    assert list(output.iterdir()) == []


def test_import_downloads_is_idempotent(tmp_path: Path) -> None:
    source = tmp_path / "downloads"
    source.mkdir()
    (source / "a.torrent").write_bytes(make_torrent(777))
    output = tmp_path / "out"

    first = Stats()
    import_downloads([source], {}, output, first)
    second = Stats()
    import_downloads([source], {}, output, second)

    assert first.downloaded == 1
    assert second.downloaded == 0
    assert second.already_exists == 1
    assert len(list(output.iterdir())) == 1
