"""Чтение метаданных .torrent. Чистый модуль: ни сети, ни файлов.

Нужен ровно для одного: достать topic_id из скачанного браузером файла.
Имя файла для этого не годится — браузер обрезает его по 255-байтному пределу
файловой системы и срезает как раз хвост ".net].t<id>.torrent".
"""

from __future__ import annotations

import re
from typing import Final

# RuTracker кладёт адрес темы в comment и publisher-url.
TOPIC_URL_KEYS: Final = (b"comment", b"publisher-url")
_TOPIC_URL_RE: Final = re.compile(r"viewtopic\.php\?t=(\d+)")
MAX_DEPTH: Final = 64

# Разбор по регулярному выражению прямо по байтам файла не годится: в bencode
# строки не разделены, и за URL сразу идёт длина следующего ключа, поэтому
# "...t=6886527" + "10:created by" дало бы topic_id 688652710. Значение
# приходится вырезать по длине, то есть разобрать bencode по-настоящему.


def _decode(data: bytes, index: int, depth: int = 0) -> tuple[object, int]:
    """Разобрать одно значение bencode, вернув его и позицию за ним."""
    if depth > MAX_DEPTH:
        raise ValueError("bencode nesting limit exceeded")
    head = data[index : index + 1]
    if head == b"i":
        end = data.index(b"e", index)
        raw = data[index + 1 : end]
        number = int(raw)
        if str(number).encode() != raw:
            raise ValueError("invalid bencode integer")
        return number, end + 1
    if head == b"l":
        index += 1
        items: list[object] = []
        while data[index : index + 1] != b"e":
            value, index = _decode(data, index, depth + 1)
            items.append(value)
        return items, index + 1
    if head == b"d":
        index += 1
        mapping: dict[bytes, object] = {}
        while data[index : index + 1] != b"e":
            key, index = _decode(data, index, depth + 1)
            if not isinstance(key, bytes) or key in mapping:
                raise ValueError("invalid bencode dictionary key")
            value, index = _decode(data, index, depth + 1)
            mapping[key] = value
        return mapping, index + 1
    colon = data.index(b":", index)
    raw_length = data[index:colon]
    length = int(raw_length)
    end = colon + 1 + length
    if length < 0 or str(length).encode() != raw_length or end > len(data):
        raise ValueError("invalid bencode string length")
    return data[colon + 1 : end], end


def topic_id_from_torrent(payload: bytes, *, strict: bool = False) -> int | None:
    """Достать topic_id из служебных полей .torrent.

    Без ссылки на тему возвращает None. Повреждённый bencode или отсутствие
    словаря info дают ValueError при strict=True, иначе также None.
    """
    try:
        decoded, end = _decode(payload, 0)
        if (
            end != len(payload)
            or not isinstance(decoded, dict)
            or not isinstance(decoded.get(b"info"), dict)
        ):
            raise ValueError("invalid torrent metadata")
    except (ValueError, IndexError, RecursionError) as exc:
        # Обрезанный или повреждённый файл — не повод ронять весь импорт.
        if strict:
            raise ValueError("invalid torrent metadata") from exc
        return None

    for key in TOPIC_URL_KEYS:
        value = decoded.get(key)
        if not isinstance(value, bytes):
            continue
        found = _TOPIC_URL_RE.search(value.decode("utf-8", errors="replace"))
        if found is not None:
            return int(found.group(1))
    return None
