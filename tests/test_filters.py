"""Тесты классификации раздач."""

from __future__ import annotations

import pytest

from rutracker_downloader.filters import (
    Classification,
    Verdict,
    classify,
    should_download,
)


@pytest.mark.parametrize(
    ("title", "forum"),
    [
        (
            "Лем Станислав - Солярис [Ковальчук Сергей, 2010, 128 kbps, MP3]",
            "Фантастика",
        ),
        ("Лем Станислав - Непобедимый (аудиокнига)", "Художественная литература"),
        ("Лем Станислав - Звёздные дневники [FLAC]", ""),
        ("Лем С. - Кибериада", "Фантастика (Аудиокниги)"),
        ("Лем Станислав - Солярис [аудиоспектакль]", ""),
        ("Лем Станислав - Маска [M4B]", ""),
        ("Лем Станислав - Эдем [320kbps]", ""),
        ("Лем Станислав - Рассказы, читает Клюквин", ""),
        ("Lem Stanislaw - Solaris [audiobook]", ""),
    ],
)
def test_audio_is_detected(title: str, forum: str) -> None:
    assert classify(title, forum).verdict is Verdict.AUDIO


@pytest.mark.parametrize(
    ("title", "forum"),
    [
        ("Лем Станислав - Собрание сочинений [FB2]", "Художественная литература"),
        ("Лем Станислав - Сумма технологии [DjVu]", ""),
        ("Лем Станислав - Солярис [EPUB, MOBI]", ""),
        ("Лем Станислав - Фиаско [PDF]", ""),
        ("Лем Станислав - Магелланово облако [doc]", ""),
        ("Лем Станислав - Дневник, найденный в ванне [txt, rtf]", ""),
        ("Лем Станислав - Сборник [AZW3]", ""),
        ("Лем Станислав - Расследование (электронная книга)", ""),
    ],
)
def test_ebook_is_detected(title: str, forum: str) -> None:
    assert classify(title, forum).verdict is Verdict.EBOOK


def test_audio_wins_over_ebook_in_mixed_release() -> None:
    """Смешанная раздача исключается: цель — не притащить аудио."""
    result = classify("Лем Станислав - Солярис [FB2, MP3]")
    assert result.verdict is Verdict.AUDIO


@pytest.mark.parametrize(
    ("title", "forum"),
    [
        ("Лем Станислав - Собрание сочинений", "Художественная литература"),
        ("Станислав Лем", ""),
        ("Лем Станислав - Солярис (2 издания)", ""),
    ],
)
def test_unknown_when_no_markers(title: str, forum: str) -> None:
    assert classify(title, forum).verdict is Verdict.UNKNOWN


@pytest.mark.parametrize(
    "title",
    [
        "Documentary about Stanislaw Lem",  # doc внутри слова
        "The paper about Lem",  # ape внутри слова
        "Докладчик о Леме",  # doc не должен ловиться в кириллице
        "Wavelet analysis of Lem",  # wav внутри слова
        "Mobile library of Lem",  # mobi внутри слова
        "Оперативная съёмка",  # ape/opus внутри слов
    ],
)
def test_substrings_do_not_trigger_markers(title: str) -> None:
    assert classify(title).verdict is Verdict.UNKNOWN


def test_marker_is_reported() -> None:
    result = classify("Лем Станислав - Солярис [MP3]")
    assert result.marker is not None
    assert result.marker.lower() == "mp3"


@pytest.mark.parametrize(
    ("verdict", "include_unknown", "expected"),
    [
        (Verdict.EBOOK, False, True),
        (Verdict.EBOOK, True, True),
        (Verdict.AUDIO, False, False),
        (Verdict.AUDIO, True, False),
        (Verdict.UNKNOWN, False, False),
        (Verdict.UNKNOWN, True, True),
    ],
)
def test_should_download(
    verdict: Verdict, include_unknown: bool, expected: bool
) -> None:
    decision = should_download(Classification(verdict), include_unknown=include_unknown)
    assert decision is expected
