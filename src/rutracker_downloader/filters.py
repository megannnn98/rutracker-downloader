"""Классификация раздач: аудио / электронная книга / неопределённо.

Чистый модуль: ни сети, ни файлов, ни логирования — только строки на входе
и вердикт на выходе. Поэтому он полностью покрывается unit-тестами.

Правило строгое (решение пользователя):
  * сработал аудио-маркер          -> AUDIO  (приоритет над всем остальным)
  * иначе сработал книжный маркер  -> EBOOK
  * иначе                          -> UNKNOWN (не скачиваем, показываем списком)

Приоритет аудио важен для смешанных раздач вида "Солярис [FB2, MP3]":
такую раздачу мы не качаем, потому что задача — исключить аудио.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

# Форматы и слова, однозначно указывающие на аудио.
# \b с обеих сторон, чтобы "ape" не ловился в "paper", а "doc" — в "document".
AUDIO_MARKERS: tuple[str, ...] = (
    r"\bmp3\b",
    r"\bm4b\b",
    r"\bm4a\b",
    r"\baac\b",
    r"\bogg\b",
    r"\bopus\b",
    r"\bflac\b",
    r"\bwav\b",
    r"\bape\b",
    r"\bwma\b",
    r"\balac\b",
    r"\bmp2\b",
    r"\baiff?\b",
    r"\bac3\b",
    r"\bdts\b",
    r"\bamr\b",
    r"\btta\b",
    r"\bwavpack\b",
    r"\baudiobook\b",
    # Живая выдача: раздел "[Аудио] Зарубежная фантастика" и "Аудиокниги (AAC, ALAC)".
    # Общий префикс покрывает аудиокнигу, аудиоспектакль, аудиопостановку и сам раздел.
    r"\bаудио\w*",
    r"\bаудио-книг\w*",
    r"\bрадиоспектакл\w*",
    r"\bначитк\w*",
    r"\bчитает\b",
    r"\bозвуч\w*",
    # Битрейт пишут слитно с числом ("320kbps"), поэтому левой границы нет.
    r"kbps\b",
    r"\bбитрейт\w*",
    r"\bvbr\b",
    r"\bcbr\b",
)

# Текстовые/книжные форматы.
EBOOK_MARKERS: tuple[str, ...] = (
    r"\bfb2\b",
    r"\bfb3\b",
    r"\bepub\b",
    r"\bpdf\b",
    r"\bdjvu?\b",
    r"\bmobi\b",
    r"\bazw3?\b",
    r"\bdocx?\b",
    r"\brtf\b",
    r"\btxt\b",
    r"\blrf\b",
    r"\bchm\b",
    r"\bebook\b",
    r"\bэлектронн\w+\s+книг\w*",
    r"\bотсканированные\s+страницы\b",
    r"\bскан\w*\s+страниц\w*",
)

_AUDIO_RE: tuple[re.Pattern[str], ...] = tuple(
    re.compile(marker, re.IGNORECASE | re.UNICODE) for marker in AUDIO_MARKERS
)
_EBOOK_RE: tuple[re.Pattern[str], ...] = tuple(
    re.compile(marker, re.IGNORECASE | re.UNICODE) for marker in EBOOK_MARKERS
)


class Verdict(StrEnum):
    """Итог классификации раздачи."""

    AUDIO = "audio"
    EBOOK = "ebook"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class Classification:
    """Вердикт плюс сработавший маркер — маркер нужен для --verbose и отладки."""

    verdict: Verdict
    marker: str | None = None


def _first_match(patterns: tuple[re.Pattern[str], ...], text: str) -> str | None:
    for pattern in patterns:
        found = pattern.search(text)
        if found is not None:
            return found.group(0)
    return None


def classify(title: str, forum: str = "") -> Classification:
    """Классифицировать раздачу по названию и названию форума."""
    haystack = f"{title}\n{forum}"

    audio = _first_match(_AUDIO_RE, haystack)
    if audio is not None:
        return Classification(Verdict.AUDIO, audio)

    ebook = _first_match(_EBOOK_RE, haystack)
    if ebook is not None:
        return Classification(Verdict.EBOOK, ebook)

    return Classification(Verdict.UNKNOWN)


def should_download(
    classification: Classification, *, include_unknown: bool = False
) -> bool:
    """Нужно ли качать раздачу с таким вердиктом."""
    if classification.verdict is Verdict.EBOOK:
        return True
    if classification.verdict is Verdict.UNKNOWN:
        return include_unknown
    return False
