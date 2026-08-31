"""Ошибки уровня приложения.

Разные причины отказа разведены по типам намеренно: Cloudflare-challenge,
протухшая сессия и сетевой сбой требуют разной реакции и разных подсказок
пользователю, и склеивать их в одно исключение значит терять диагностику.
"""

from __future__ import annotations


class RutrackerError(Exception):
    """Базовая ошибка утилиты."""


class ConfigError(RutrackerError):
    """Не хватает конфигурации: cookies, User-Agent, каталог вывода."""


class CloudflareChallenge(RutrackerError):
    """Cloudflare вернул interstitial-challenge вместо страницы.

    Ретраить бессмысленно: нужен свежий cf_clearance из браузера.
    """


class SessionExpired(RutrackerError):
    """Cloudflare пропустил, но RuTracker считает нас гостем (bb_session протух)."""


class LoginError(RutrackerError):
    """Логин по паролю не удался."""


class CaptchaRequired(LoginError):
    """RuTracker потребовал CAPTCHA; обход не реализуется."""


class HttpError(RutrackerError):
    """HTTP-ошибка, пережившая ретраи."""


class TorrentUnavailable(RutrackerError):
    """У раздачи нет ссылки на .torrent или ответ не является torrent-файлом."""
