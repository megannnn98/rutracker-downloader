"""Снять живые страницы выдачи в tests/fixtures и проверить селекторы.

Запускается один раз, до написания парсера: селекторы фиксируются по факту,
а не по памяти. Требует cookies.txt, экспортированный из браузера, и
RUTRACKER_USER_AGENT, совпадающий с этим браузером.

    uv run python scripts/dump_fixtures.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import NoReturn
from urllib.parse import urljoin

from selectolax.parser import HTMLParser

from rutracker_downloader.client import RutrackerClient
from rutracker_downloader.config import load_settings
from rutracker_downloader.errors import RutrackerError

QUERY = "станислав лем"
FIXTURES_DIR = Path("tests/fixtures")

# Кандидаты в селекторы: сверяем таблицу из брифа с реальной вёрсткой.
SELECTOR_CANDIDATES: tuple[str, ...] = (
    "table#tor-tbl",
    "tr[data-topic_id]",
    "tr[id^=trs-tr-]",
    "a.tLink",
    "a.ts-text",
    "a.f-name",
    "a.tr-dl",
    'a[href^="dl.php?t="]',
    'a[href^="viewtopic.php?t="]',
    "td.tor-size",
    "b.seedmed",
)

# Что вычищаем из фикстур: ник, идентификаторы сессии, ссылки выхода.
SANITIZE_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"sid=[0-9a-f]+", "sid=REDACTED"),
    (r"logout=[^\"'&]+", "logout=REDACTED"),
    (r"form_token\s*:\s*'[^']*'", "form_token: 'REDACTED'"),
    (r"cap_sid=[^\"'&]+", "cap_sid=REDACTED"),
)


def fail(message: str) -> NoReturn:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


# Короткие значения кук (у RuTracker есть кука со значением "1") нельзя
# вырезать подстановкой: она изувечит разметку — row1 -> rowREDACTED.
MIN_SECRET_LEN = 12


def sanitize(
    html: str, extra_secrets: list[str], login_secrets: frozenset[str] = frozenset()
) -> str:
    cleaned = html
    for pattern, replacement in SANITIZE_PATTERNS:
        cleaned = re.sub(pattern, replacement, cleaned, flags=re.IGNORECASE)
    for secret in extra_secrets:
        if len(secret) >= MIN_SECRET_LEN or secret in login_secrets:
            cleaned = cleaned.replace(secret, "REDACTED")
    return cleaned


def pagination_urls(html: str, page_url: str) -> list[str]:
    tree = HTMLParser(html)
    urls: set[str] = set()
    for node in tree.css("a[href]"):
        href = node.attributes.get("href")
        if href and "tracker.php" in href and "start=" in href:
            urls.add(urljoin(page_url, href))
    return sorted(urls)


def report(html: str, page_url: str, label: str) -> None:
    tree = HTMLParser(html)
    print(f"\n--- {label} ---")
    for selector in SELECTOR_CANDIDATES:
        print(f"  {selector:<32} {len(tree.css(selector))}")
    print(f"  {'pagination links':<32} {len(pagination_urls(html, page_url))}")
    first_row = tree.css_first("tr[data-topic_id]")
    if first_row is not None:
        print("  первая строка (обрезано):")
        print("   ", first_row.html[:400] if first_row.html else "")


def main() -> None:
    try:
        settings = load_settings()
    except RutrackerError as exc:
        fail(str(exc))

    cookie_values: list[str] = []
    login_credentials = frozenset(
        c for c in (settings.username, settings.password) if c
    )
    try:
        with RutrackerClient(settings, delay=1.5) as client:
            first_url = client.search_url(QUERY)
            first_html = client.fetch_page(first_url)
            cookie_values = client.cookie_values()
            # Ник виден в шапке залогиненной страницы, пароль — на всякий случай.
            for credential in (settings.username, settings.password):
                if credential:
                    cookie_values.append(credential)

            pages = pagination_urls(first_html, first_url)
            second_url = next((u for u in pages if u != first_url), None)
            second_html = client.fetch_page(second_url) if second_url else None
    except RutrackerError as exc:
        fail(str(exc))

    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    (FIXTURES_DIR / "search_page1.html").write_text(
        sanitize(first_html, cookie_values, login_credentials), encoding="utf-8"
    )
    report(first_html, first_url, "page 1")

    if second_html is None:
        print("\nвторой страницы в выдаче нет (или ссылка не найдена)")
    else:
        (FIXTURES_DIR / "search_page2.html").write_text(
            sanitize(second_html, cookie_values, login_credentials), encoding="utf-8"
        )
        report(second_html, second_url or "", "page 2")


if __name__ == "__main__":
    main()
