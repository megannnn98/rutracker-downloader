"""Экспорт cookies rutracker из профиля Firefox в Netscape-файл.

Запускать самому пользователю: агенту доступ к профилю браузера закрыт
permission-правилами, и это правильно.

    uv run python scripts/export_firefox_cookies.py

Firefox держит cookies.sqlite залоченным, поэтому работаем с копией.
Значения кук не печатаются — только имена.
"""

from __future__ import annotations

import argparse
import glob
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

REQUIRED = ("cf_clearance", "bb_session")


def find_profile_db(explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit
    candidates = glob.glob(os.path.expanduser("~/.mozilla/firefox/*/cookies.sqlite"))
    candidates += glob.glob(
        os.path.expanduser("~/snap/firefox/common/.mozilla/firefox/*/cookies.sqlite")
    )
    candidates += glob.glob(
        os.path.expanduser(
            "~/.var/app/org.mozilla.firefox/.mozilla/firefox/*/cookies.sqlite"
        )
    )
    if not candidates:
        sys.exit("не найден cookies.sqlite; укажите путь через --db")
    return Path(max(candidates, key=os.path.getmtime))


def read_cookies(db: Path, domain: str) -> list[tuple[str, str, int, int, str, str]]:
    workdir = Path(tempfile.mkdtemp())
    copy = workdir / "cookies.sqlite"
    for suffix in ("", "-wal", "-shm"):
        source = Path(str(db) + suffix)
        if source.exists():
            shutil.copy2(source, str(copy) + suffix)
    with sqlite3.connect(copy) as connection:
        rows = connection.execute(
            "SELECT host, path, isSecure, expiry, name, value "
            "FROM moz_cookies WHERE host LIKE ?",
            (f"%{domain}%",),
        ).fetchall()
    shutil.rmtree(workdir, ignore_errors=True)
    return list(rows)


def write_netscape(
    rows: list[tuple[str, str, int, int, str, str]], target: Path
) -> None:
    lines = ["# Netscape HTTP Cookie File"]
    for host, path, secure, expiry, name, value in rows:
        lines.append(
            "\t".join(
                [
                    host,
                    "TRUE" if host.startswith(".") else "FALSE",
                    path,
                    "TRUE" if secure else "FALSE",
                    str(expiry),
                    name,
                    value,
                ]
            )
        )
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    target.chmod(0o600)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=None, help="путь к cookies.sqlite")
    parser.add_argument("--domain", default="rutracker", help="подстрока домена")
    parser.add_argument(
        "--out", type=Path, default=Path("cookies.txt"), help="куда писать"
    )
    args = parser.parse_args()

    db = find_profile_db(args.db)
    rows = read_cookies(db, args.domain)
    if not rows:
        sys.exit(
            f"в {db} нет кук домена {args.domain}: зайдите на форум в Firefox и повторите"
        )

    write_netscape(rows, args.out)
    names = sorted(row[4] for row in rows)
    print(f"профиль : {db}")
    print(f"записано: {len(rows)} кук -> {args.out}")
    print(f"имена   : {names}")

    missing = [name for name in REQUIRED if name not in names]
    if missing:
        print(f"ВНИМАНИЕ: нет {missing}. Откройте в Firefox")
        print("  https://rutracker.net/forum/tracker.php?nm=тест")
        print("дождитесь прохождения проверки Cloudflare и запустите скрипт заново.")


if __name__ == "__main__":
    main()
