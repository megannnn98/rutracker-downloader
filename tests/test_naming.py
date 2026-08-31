"""Тесты построения имён файлов."""

from __future__ import annotations

from rutracker_downloader.naming import MAX_FILENAME_BYTES, torrent_filename


def test_slash_is_not_a_directory_separator() -> None:
    name = torrent_filename(123, "Лем / Солярис")
    assert "/" not in name
    assert name.endswith(".torrent")


def test_control_characters_are_removed() -> None:
    name = torrent_filename(123, "Лем\x00\nСолярис")
    assert "\x00" not in name
    assert "\n" not in name


def test_name_starts_with_topic_id() -> None:
    assert torrent_filename(1234567, "Солярис").startswith("1234567_")


def test_long_title_is_truncated_within_filesystem_limit() -> None:
    name = torrent_filename(1234567, "Лем " * 300)
    assert len(name.encode("utf-8")) <= MAX_FILENAME_BYTES
    assert name.endswith(".torrent")


def test_truncation_keeps_valid_utf8() -> None:
    name = torrent_filename(1, "я" * 300)
    name.encode("utf-8").decode("utf-8")


def test_empty_title_still_produces_a_name() -> None:
    assert torrent_filename(42, "   ") == "42.torrent"


def test_leading_dot_does_not_create_hidden_file() -> None:
    assert not torrent_filename(42, "...Солярис").startswith(".")


def test_same_title_gives_same_name() -> None:
    """Идемпотентность: повторный запуск должен найти уже скачанный файл."""
    assert torrent_filename(7, "Лем - Солярис [FB2]") == torrent_filename(
        7, "Лем - Солярис [FB2]"
    )
