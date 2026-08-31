"""Оркестрация: обход выдачи, фильтрация, идемпотентное скачивание."""

from __future__ import annotations

import logging
import os
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from rutracker_downloader.errors import HttpError, TorrentUnavailable
from rutracker_downloader.filters import Verdict, classify, should_download
from rutracker_downloader.models import TorrentEntry
from rutracker_downloader.naming import torrent_filename
from rutracker_downloader.parser import parse_search_page

logger = logging.getLogger(__name__)

DEFAULT_MAX_PAGES = 50


class SearchClient(Protocol):
    """Минимальный контракт клиента.

    Протокол, а не конкретный RutrackerClient: оркестрация тестируется
    подставным клиентом, без сети и без моков httpx.
    """

    def search_url(self, query: str, start: int = 0) -> str: ...

    def fetch_page(self, url: str) -> str: ...

    def download_torrent(self, topic_id: int) -> bytes: ...


@dataclass(slots=True)
class Stats:
    """Счётчики прогона. Печатаются даже при досрочной остановке."""

    found: int = 0
    audio_excluded: int = 0
    unknown_skipped: int = 0
    downloaded: int = 0
    already_exists: int = 0
    missing_link: int = 0
    errors: int = 0
    pages: int = 0
    duplicates: int = 0
    unknown_titles: list[str] = field(default_factory=list)

    def render(self) -> str:
        return "\n".join(
            [
                f"страниц выдачи обработано : {self.pages}",
                f"найдено раздач           : {self.found}",
                f"дубликатов пропущено     : {self.duplicates}",
                f"исключено как аудио      : {self.audio_excluded}",
                f"неопределённых пропущено : {self.unknown_skipped}",
                f"без ссылки на .torrent   : {self.missing_link}",
                f"скачано новых            : {self.downloaded}",
                f"уже существовало         : {self.already_exists}",
                f"ошибок                   : {self.errors}",
            ]
        )


class Downloader:
    """Связывает клиент, парсер и фильтр в один прогон."""

    def __init__(
        self,
        client: SearchClient,
        output_dir: Path,
        *,
        include_unknown: bool = False,
        dry_run: bool = False,
        max_pages: int = DEFAULT_MAX_PAGES,
    ) -> None:
        self._client = client
        self._output_dir = output_dir
        self._include_unknown = include_unknown
        self._dry_run = dry_run
        self._max_pages = max_pages
        self.stats = Stats()

    def iter_entries(self, query: str) -> Iterator[TorrentEntry]:
        """Обойти страницы выдачи, отдавая уникальные раздачи."""
        pending = [self._client.search_url(query)]
        visited: set[str] = set()
        seen_topics: set[int] = set()

        while pending and self.stats.pages < self._max_pages:
            url = pending.pop(0)
            if url in visited:
                continue
            visited.add(url)

            html = self._client.fetch_page(url)
            page = parse_search_page(html, url)
            self.stats.pages += 1
            logger.debug("страница %s: раздач %d", url, len(page.entries))

            for candidate in page.pagination_urls:
                if candidate not in visited and candidate not in pending:
                    pending.append(candidate)

            for entry in page.entries:
                self.stats.found += 1
                if entry.topic_id in seen_topics:
                    self.stats.duplicates += 1
                    continue
                seen_topics.add(entry.topic_id)
                yield entry

        if pending:
            logger.warning(
                "достигнут лимит --max-pages=%d, осталось необойдённых страниц: %d",
                self._max_pages,
                len(pending),
            )

    def _save(self, entry: TorrentEntry, payload: bytes) -> bool:
        """Создать файл атомарно, не перезаписывая существующий.

        Временный файл уникален для процесса, а os.link создаёт цель только
        если её ещё нет. Это переживает параллельные запуски в один каталог:
        победитель ровно один, проигравший видит FileExistsError и считает
        раздачу уже скачанной вместо того, чтобы затирать чужой результат.
        """
        target = self._output_dir / torrent_filename(entry.topic_id, entry.title)
        handle, temporary_name = tempfile.mkstemp(
            dir=self._output_dir, prefix=f".{entry.topic_id}-", suffix=".part"
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(payload)
            try:
                os.link(temporary, target)
            except FileExistsError:
                logger.debug("файл уже создан параллельным запуском: %s", target.name)
                return False
        finally:
            temporary.unlink(missing_ok=True)

        logger.info("скачано: %s", target.name)
        return True

    def _handle(self, entry: TorrentEntry) -> None:
        target = self._output_dir / torrent_filename(entry.topic_id, entry.title)
        if target.exists() and target.stat().st_size > 0:
            self.stats.already_exists += 1
            logger.debug("уже есть: %s", target.name)
            return

        if entry.download_url is None:
            self.stats.missing_link += 1
            logger.warning(
                "нет ссылки на .torrent: %s (%s)", entry.title, entry.topic_url
            )
            return

        if self._dry_run:
            print(
                f"  [dry-run] {entry.topic_id}  {entry.title}  <- {entry.download_url}"
            )
            return

        try:
            payload = self._client.download_torrent(entry.topic_id)
        except (TorrentUnavailable, HttpError) as exc:
            self.stats.errors += 1
            logger.error("не скачано %s: %s", entry.topic_id, exc)
            return

        if self._save(entry, payload):
            self.stats.downloaded += 1
        else:
            self.stats.already_exists += 1

    def run(self, query: str) -> Stats:
        """Выполнить прогон. Ошибки уровня сессии пробрасываются наружу."""
        if not self._dry_run:
            self._output_dir.mkdir(parents=True, exist_ok=True)

        # Сессионные ошибки (challenge, протухший bb_session) намеренно не ловятся:
        # прогон обрывается, а накопленную статистику печатает CLI.
        for entry in self.iter_entries(query):
            verdict = classify(entry.title, entry.forum)

            if verdict.verdict is Verdict.AUDIO:
                self.stats.audio_excluded += 1
                logger.debug("аудио (%s): %s", verdict.marker, entry.title)
                continue

            if verdict.verdict is Verdict.UNKNOWN:
                self.stats.unknown_titles.append(entry.title)

            if not should_download(verdict, include_unknown=self._include_unknown):
                self.stats.unknown_skipped += 1
                continue

            self._handle(entry)

        return self.stats
