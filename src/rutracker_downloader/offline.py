"""Офлайн-режим: разбор сохранённых из браузера страниц выдачи.

Если сетевой клиент не получает выдачу, страницы сохраняются браузером,
а утилита берёт на себя всё остальное:
разбор, фильтрацию, дедупликацию и список ссылок на .torrent для качалки.

Модуль читает только локальные файлы: сети здесь нет.
"""

from __future__ import annotations

import logging
from collections.abc import Container, Mapping, Sequence
from pathlib import Path
from typing import Final

from rutracker_downloader.client import is_torrent_payload
from rutracker_downloader.downloader import Stats, save_payload, select_entries
from rutracker_downloader.models import TorrentEntry
from rutracker_downloader.naming import torrent_filename
from rutracker_downloader.parser import parse_search_page
from rutracker_downloader.torrent_file import topic_id_from_torrent

logger = logging.getLogger(__name__)

HTML_SUFFIXES: Final = (".html", ".htm")


def collect_html_files(paths: Sequence[Path]) -> list[Path]:
    """Развернуть каталоги в отсортированный список html-файлов.

    Сортировка делает порядок обхода воспроизводимым: иначе номер страницы в
    логе зависел бы от порядка выдачи файловой системы.
    """
    files: list[Path] = []
    for path in paths:
        if path.is_dir():
            files.extend(
                sorted(
                    child
                    for child in path.iterdir()
                    if child.is_file() and child.suffix.lower() in HTML_SUFFIXES
                )
            )
        else:
            files.append(path)
    return files


def read_page(path: Path) -> str:
    """Прочитать сохранённую страницу, определив кодировку.

    RuTracker отдаёт cp1251, и «Сохранить как» кладёт байты как есть, но
    часть инструментов (devtools, wget с конвертацией) пишет уже UTF-8.
    Пробуем UTF-8 строго: последовательности cp1251 почти никогда не образуют
    валидный UTF-8, так что неудача декодирования — надёжный признак cp1251.
    """
    raw = path.read_bytes()
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1251", errors="replace")


def plan_downloads(
    paths: Sequence[Path],
    *,
    search_url: str,
    include_unknown: bool,
    stats: Stats,
    require_download_link: bool = True,
) -> list[TorrentEntry]:
    """Разобрать сохранённые страницы и отобрать раздачи к скачиванию.

    search_url нужен как база для urljoin: в выдаче ссылки относительные
    ("dl.php?t=..."), и без базы они не превратятся в рабочий URL.
    """
    entries: list[TorrentEntry] = []
    for path in collect_html_files(paths):
        page = parse_search_page(read_page(path), search_url)
        stats.pages += 1
        if not page.entries:
            # Молча пропустить нельзя: чаще всего это сохранённая страница
            # проверки Cloudflare или гостевая, и пользователь ждёт от неё раздач.
            logger.warning(
                "%s: ни одной раздачи — это точно страница выдачи, а не проверка "
                "Cloudflare или страница гостя?",
                path.name,
            )
        logger.debug("%s: раздач %d", path.name, len(page.entries))
        entries.extend(page.entries)

    selected = select_entries(entries, stats, include_unknown=include_unknown)

    planned: list[TorrentEntry] = []
    for entry in selected:
        if entry.download_url is None:
            stats.missing_link += 1
            logger.warning("нет ссылки на .torrent: %s", entry.title)
            if require_download_link:
                continue
        planned.append(entry)
    return planned


def write_links(entries: Sequence[TorrentEntry], target: Path) -> None:
    """Записать ссылки по одной в строке — формат, понятный любой качалке."""
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = [entry.download_url for entry in entries if entry.download_url]
    target.write_text(("\n".join(lines) + "\n") if lines else "", encoding="utf-8")


TORRENT_SUFFIX: Final = ".torrent"


def collect_torrent_files(paths: Sequence[Path]) -> list[Path]:
    """Развернуть каталоги в отсортированный список .torrent-файлов."""
    files: list[Path] = []
    for path in paths:
        if path.is_dir():
            files.extend(
                sorted(
                    child
                    for child in path.iterdir()
                    if child.is_file() and child.suffix.lower() == TORRENT_SUFFIX
                )
            )
        else:
            files.append(path)
    return files


def import_downloads(
    sources: Sequence[Path],
    titles: Mapping[int, str],
    output_dir: Path,
    stats: Stats,
    *,
    allowed: Container[int] | None = None,
    dry_run: bool = False,
) -> None:
    """Разложить скачанные браузером .torrent по схеме именования проекта.

    titles берутся из разбора сохранённых страниц: в самом файле названия
    раздачи нет, а имя, под которым его сохранил браузер, обрезано. Без
    названия остаётся осмысленный запасной вариант — имя из одного topic_id.

    allowed ограничивает импорт раздачами, прошедшими фильтр. Браузерный
    сниппет качает выдачу целиком, не разбирая: отделять аудио от книг в JS
    значило бы завести вторую копию правил, которая разойдётся с filters.py.
    None означает «разложить всё» — режим импорта без разбора страниц.
    """
    if not dry_run:
        output_dir.mkdir(parents=True, exist_ok=True)

    for path in collect_torrent_files(sources):
        payload = path.read_bytes()
        if not is_torrent_payload(payload):
            # Нулевые файлы от оборвавшихся загрузок сюда же: тихо пропустить
            # их нельзя, иначе пользователь не поймёт, почему раздачи нет.
            stats.errors += 1
            logger.warning(
                "не .torrent, пропущен: %s (%d байт)", path.name, len(payload)
            )
            continue

        try:
            topic_id = topic_id_from_torrent(payload, strict=True)
        except ValueError:
            stats.errors += 1
            logger.warning("повреждённый .torrent, пропущен: %s", path.name)
            continue
        if topic_id is None:
            logger.info("в файле нет ссылки на тему, пропущен: %s", path.name)
            continue

        if allowed is not None and topic_id not in allowed:
            # Не ошибка: раздачу отсеял фильтр, и она уже посчитана в статистике
            # разбора страниц как аудио или неопределённая.
            logger.debug("не прошёл фильтр, пропущен: %s", path.name)
            continue

        filename = torrent_filename(topic_id, titles.get(topic_id, ""))
        if dry_run:
            print(f"  [dry-run] {path.name} -> {filename}")
            continue
        if save_payload(output_dir, filename, payload, prefix=f".{topic_id}-"):
            stats.downloaded += 1
        else:
            stats.already_exists += 1
