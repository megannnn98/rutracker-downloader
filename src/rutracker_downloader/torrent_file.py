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

# Разбор по регулярному выражению прямо по байтам файла не годится: в bencode
# строки не разделены, и за URL сразу идёт длина следующего ключа, поэтому
# "...t=6886527" + "10:created by" дало бы topic_id 688652710. Значение
# приходится вырезать по длине, то есть разобрать bencode по-настоящему.


def _decode(data: bytes, index: int) -> tuple[object, int]:
    """Разобрать одно значение bencode, вернув его и позицию за ним."""
    head = data[index : index + 1]
    if head == b"i":
        end = data.index(b"e", index)
        return int(data[index + 1 : end]), end + 1
    if head == b"l":
        index += 1
        items: list[object] = []
        while data[index : index + 1] != b"e":
            value, index = _decode(data, index)
            items.append(value)
        return items, index + 1
    if head == b"d":
        index += 1
        mapping: dict[bytes, object] = {}
        while data[index : index + 1] != b"e":
            key, index = _decode(data, index)
            value, index = _decode(data, index)
            if isinstance(key, bytes):
                mapping[key] = value
        return mapping, index + 1
    colon = data.index(b":", index)
    length = int(data[index:colon])
    return data[colon + 1 : colon + 1 + length], colon + 1 + length


def topic_id_from_torrent(payload: bytes) -> int | None:
    """Достать topic_id из служебных полей .torrent.

    None означает, что файл разобрался, но ссылки на тему в нём нет: такой
    .torrent мог быть создан не на RuTracker.
    """
    try:
        decoded, _ = _decode(payload, 0)
    except (ValueError, IndexError):
        # Обрезанный или повреждённый файл — не повод ронять весь импорт.
        return None
    if not isinstance(decoded, dict):
        return None

    for key in TOPIC_URL_KEYS:
        value = decoded.get(key)
        if not isinstance(value, bytes):
            continue
        found = _TOPIC_URL_RE.search(value.decode("utf-8", errors="replace"))
        if found is not None:
            return int(found.group(1))
    return None
