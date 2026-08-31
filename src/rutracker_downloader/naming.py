"""Построение безопасных имён файлов. Чистый модуль без IO."""

from __future__ import annotations

import re
import unicodedata

# Максимальная длина имени файла в байтах. В ext4/btrfs предел 255 байт,
# берём запас на суффикс ".part" и на кириллицу (2 байта на символ в UTF-8).
MAX_FILENAME_BYTES = 200

TORRENT_SUFFIX = ".torrent"

# Символы, недопустимые или неудобные в имени файла на Linux:
# NUL и управляющие, слэши, а также кавычки/двоеточия, ломающие копипаст в shell.
_UNSAFE_RE = re.compile(r'[\x00-\x1f\x7f/\\:*?"<>|]+')
_SPACE_RE = re.compile(r"\s+")
_REPEAT_RE = re.compile(r"_{2,}")


def _truncate_bytes(value: str, limit: int) -> str:
    """Обрезать строку до limit байт UTF-8, не разрывая символ."""
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value
    return encoded[:limit].decode("utf-8", errors="ignore").rstrip("_")


def slugify(title: str) -> str:
    """Превратить название раздачи в безопасный фрагмент имени файла."""
    normalized = unicodedata.normalize("NFC", title)
    cleaned = _UNSAFE_RE.sub("_", normalized)
    cleaned = _SPACE_RE.sub("_", cleaned)
    cleaned = _REPEAT_RE.sub("_", cleaned)
    # Ведущие точки создали бы скрытый файл, ведущий дефис — путаницу с флагами.
    cleaned = cleaned.strip("._-")
    return cleaned


def torrent_filename(topic_id: int, title: str) -> str:
    """Имя файла вида "1234567_Название.torrent".

    topic_id идёт первым и гарантирует уникальность: два разных топика с
    одинаковым названием не столкнутся, а повторный запуск найдёт тот же файл.
    """
    prefix = str(topic_id)
    budget = MAX_FILENAME_BYTES - len(prefix) - len(TORRENT_SUFFIX) - 1
    slug = _truncate_bytes(slugify(title), max(budget, 0))
    if not slug:
        return f"{prefix}{TORRENT_SUFFIX}"
    return f"{prefix}_{slug}{TORRENT_SUFFIX}"
