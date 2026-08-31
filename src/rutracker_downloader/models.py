"""Модели данных. Без сети и IO."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TorrentEntry:
    """Одна раздача из результатов поиска."""

    topic_id: int
    title: str
    forum: str
    topic_url: str
    # None означает, что в строке выдачи не было ссылки на .torrent
    # (раздача поглощена/недооформлена) — это отдельный случай, не ошибка сети.
    download_url: str | None = None


@dataclass(frozen=True, slots=True)
class SearchPage:
    """Результат разбора одной страницы выдачи."""

    entries: tuple[TorrentEntry, ...]
    pagination_urls: tuple[str, ...]
