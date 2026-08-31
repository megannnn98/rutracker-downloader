"""Конфигурация из окружения / .env. Учётные данные никогда не хардкодятся."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from rutracker_downloader.errors import ConfigError

DEFAULT_BASE_URL = "https://rutracker.net"
DEFAULT_COOKIES_FILE = Path("./cookies.txt")


@dataclass(frozen=True, slots=True)
class Settings:
    """Настройки запуска.

    user_agent обязателен и должен совпадать с браузером, из которого
    экспортированы cookies: Cloudflare привязывает cf_clearance к паре
    IP + User-Agent, и при несовпадении вернётся challenge.
    """

    base_url: str
    user_agent: str
    cookies_file: Path
    username: str | None = None
    password: str | None = None

    @property
    def login_url(self) -> str:
        return f"{self.base_url}/forum/login.php"

    @property
    def search_url(self) -> str:
        return f"{self.base_url}/forum/tracker.php"

    def topic_url(self, topic_id: int) -> str:
        return f"{self.base_url}/forum/viewtopic.php?t={topic_id}"

    def download_url(self, topic_id: int) -> str:
        return f"{self.base_url}/forum/dl.php?t={topic_id}"


def load_settings(
    *,
    base_url: str | None = None,
    cookies_file: Path | None = None,
) -> Settings:
    """Собрать настройки; аргументы CLI имеют приоритет над окружением."""
    load_dotenv()

    user_agent = os.environ.get("RUTRACKER_USER_AGENT", "").strip()
    if not user_agent:
        raise ConfigError(
            "не задан RUTRACKER_USER_AGENT. Укажите в .env строку User-Agent того "
            "браузера, из которого экспортированы cookies "
            "(Firefox: about:support -> «Строка агента пользователя»)."
        )

    resolved_cookies = cookies_file or Path(
        os.environ.get("RUTRACKER_COOKIES", str(DEFAULT_COOKIES_FILE))
    )
    resolved_base = (
        base_url or os.environ.get("RUTRACKER_BASE_URL", DEFAULT_BASE_URL)
    ).rstrip("/")

    return Settings(
        base_url=resolved_base,
        user_agent=user_agent,
        cookies_file=resolved_cookies,
        username=os.environ.get("RUTRACKER_USERNAME") or None,
        password=os.environ.get("RUTRACKER_PASSWORD") or None,
    )
