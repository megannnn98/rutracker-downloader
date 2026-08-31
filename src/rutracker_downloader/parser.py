"""Разбор HTML выдачи RuTracker. Чистый модуль: ни сети, ни файлов.

Селекторы взяты из рабочих сторонних парсеров (qBittorrent-плагин, Jackett,
rutracker-monitor) и проверяются на сохранённых фикстурах в tests/.
Для каждого поля есть цепочка кандидатов: вёрстка форума менялась, и
единственный жёсткий селектор — самая частая причина тихой поломки парсера.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urljoin, urlsplit

from selectolax.parser import HTMLParser, Node

from rutracker_downloader.models import SearchPage, TorrentEntry

ROW_SELECTORS: tuple[str, ...] = (
    "table#tor-tbl tr[data-topic_id]",
    "tr[data-topic_id]",
)
TITLE_SELECTORS: tuple[str, ...] = (
    "a.tLink",
    "a.ts-text",
    'a[href*="viewtopic.php?t="]',
)
# Проверено на живой выдаче: категория лежит в <td class="f-name-col">
# <div class="f-name"><a class="gen f ts-text" href="tracker.php?f=NNNN">.
# Селектора a.f-name, встречающегося в старых парсерах, на странице нет.
FORUM_SELECTORS: tuple[str, ...] = (
    "td.f-name-col a",
    "div.f-name a",
    'a[href*="tracker.php?f="]',
)
DOWNLOAD_SELECTORS: tuple[str, ...] = (
    "a.tr-dl",
    'a[href^="dl.php?t="]',
    'a[href*="dl.php?t="]',
)

_TOPIC_ID_RE = re.compile(r"[?&]t=(\d+)")

logger = logging.getLogger(__name__)


def _origin(url: str) -> tuple[str, str]:
    parts = urlsplit(url)
    return parts.scheme, parts.netloc


def _first(row: Node, selectors: tuple[str, ...]) -> Node | None:
    for selector in selectors:
        found = row.css_first(selector)
        if found is not None:
            return found
    return None


def _text(node: Node | None) -> str:
    return node.text(strip=True) if node is not None else ""


def _href(node: Node | None, base_url: str) -> str | None:
    if node is None:
        return None
    href = node.attributes.get("href")
    return urljoin(base_url, href) if href else None


def _topic_id(row: Node, title_href: str | None) -> int | None:
    raw = row.attributes.get("data-topic_id")
    if raw is not None and raw.isdigit():
        return int(raw)
    if title_href is not None:
        found = _TOPIC_ID_RE.search(title_href)
        if found is not None:
            return int(found.group(1))
    return None


def parse_row(row: Node, base_url: str) -> TorrentEntry | None:
    """Разобрать одну строку таблицы. None, если строка не про раздачу."""
    title_node = _first(row, TITLE_SELECTORS)
    title_href = _href(title_node, base_url)
    topic_id = _topic_id(row, title_href)
    if topic_id is None:
        return None

    title = _text(title_node)
    if not title:
        return None

    return TorrentEntry(
        topic_id=topic_id,
        title=title,
        forum=_text(_first(row, FORUM_SELECTORS)),
        topic_url=title_href or urljoin(base_url, f"viewtopic.php?t={topic_id}"),
        download_url=_href(_first(row, DOWNLOAD_SELECTORS), base_url),
    )


def extract_pagination_urls(tree: HTMLParser, page_url: str) -> tuple[str, ...]:
    """Ссылки на другие страницы выдачи.

    Берём их из HTML, а не конструируем: RuTracker может подмешивать search_id,
    и собранный вручную URL молча вернёт не ту выдачу.
    """
    origin = _origin(page_url)
    urls: list[str] = []
    seen: set[str] = set()
    for node in tree.css("a[href]"):
        href = node.attributes.get("href")
        if not href or "tracker.php" not in href or "start=" not in href:
            continue
        absolute = urljoin(page_url, href)
        # Абсолютный href на чужой хост downloader позже честно запросил бы,
        # выйдя за границу RuTracker. Ограничиваем обход тем же origin.
        if _origin(absolute) != origin:
            logger.warning("пагинация на чужой origin пропущена: %s", absolute)
            continue
        if absolute not in seen:
            seen.add(absolute)
            urls.append(absolute)
    return tuple(urls)


def parse_search_page(html: str, page_url: str) -> SearchPage:
    """Разобрать страницу выдачи: раздачи и ссылки пагинации."""
    tree = HTMLParser(html)

    rows: list[Node] = []
    for selector in ROW_SELECTORS:
        rows = tree.css(selector)
        if rows:
            break

    entries: list[TorrentEntry] = []
    for row in rows:
        entry = parse_row(row, page_url)
        if entry is not None:
            entries.append(entry)

    return SearchPage(
        entries=tuple(entries),
        pagination_urls=extract_pagination_urls(tree, page_url),
    )
