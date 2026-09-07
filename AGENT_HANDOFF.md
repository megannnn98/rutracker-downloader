# Handoff: RuTracker downloader

## Goal

Continue only under explicit user direction. Current investigation established
that RuTracker Cloudflare rejects both ordinary HTTP clients and Selenium
WebDriver. Do not attempt fingerprint spoofing or anti-bot evasion.

## User requirements

- Respond in Russian. User enabled `caveman` full mode: terse technical replies.
- First prove root cause; then propose minimal patch. No refactoring without an
  explicit request.
- Before any non-trivial edit, build, documentation change, install, or system
  action: give a three-step plan and wait for explicit `go`.
- Never print secrets. `.env` and `cookies.txt` contain sensitive session data.
- Use `vim`, not `nano`, when instructing user about editor commands.
- At end of every response update
  `~/Documents/obsidian/torrent-loader/context-current.md`.

## Current status

**VERIFIED:** project code is back at `HEAD`; current `git diff` is empty.
Only pre-existing untracked `.env.example` and this handoff file are expected
after creating this document.

**VERIFIED:** `ruff`, `mypy --strict`, and `pytest` passed after rollback;
pytest reported `121 passed`.

**VERIFIED from user-provided evidence:** Firefox can open
`https://rutracker.net/forum/tracker.php?nm=кант` after Cloudflare. Freshly
exported `cookies.txt` contained `cf_clearance` and `bb_session`, while the
CLI immediately received HTTP 403 / JS challenge.

**VERIFIED locally:** `.env` User-Agent exactly matched installed Firefox
154.0.1 default UA. Cookie metadata had `.rutracker.net` domain and applicable
paths. This is not a missing-cookie, wrong-domain, or User-Agent mismatch.

## Root cause

**VERIFIED:** Cloudflare distinguishes client fingerprint beyond cookies and
User-Agent. `httpx` fails before first results page. Browser automation is not
a safe alternative:

- A temporary Selenium Firefox transport was implemented, tested, and then
  fully reverted. Live run showed `Cloudflare не принял автоматизированную
  сессию Firefox` before page 1.
- The legacy qBittorrent RuTracker plugin was temporarily installed and then
  removed. qBittorrent log showed `urllib.error.HTTPError: HTTP Error 403:
  Forbidden` in `rutracker.py:124`, during `__login()` to `login.php`, before
  credentials were evaluated.

Do not retry Selenium, Playwright, user-agent/header spoofing, or other
fingerprint-masking approaches. Such work was rejected as unreliable and out
of scope by the prior investigation.

## Code ground truth

Verify against these files, not this handoff:

- `src/rutracker_downloader/client.py:51-55` user-facing Cloudflare guidance.
- `src/rutracker_downloader/client.py:62-95` newest user/cache cookie jar
  selection and loading.
- `src/rutracker_downloader/client.py:255-270` request path; challenge is
  deliberately not retried.
- `src/rutracker_downloader/config.py:47-76` configuration and required
  User-Agent.
- `docs/authentication.md:3-22` documented limitation of normal HTTP clients.
- `docs/authentication.md:24-65` Firefox cookie export and cache behaviour.
- `pyproject.toml:9-14` current dependencies: no Selenium or browser driver.

## Repository state

- Repository: `/home/b/Documents/torrent-loader`
- Branch: `main`
- HEAD: `ea6168ce3bab3fe282350aef6a4f58ddf3ab7536`
  (`feat: asyncio-миграция, параллелизм обхода/скачивания, дефолты --delay 0.01 / --concurrency 20`)
- Pre-existing untracked file: `.env.example`. Do not add, print, or modify
  it. Prior review found real credentials in it; user should rotate them.
- No `.codegraph/` directory.
- No staged or modified tracked files before this handoff was created.

## Changes made in this session

- Temporary Selenium browser transport, tests, dependency, docs, and lockfile
  updates were fully removed. There must be no remnants in code or lockfile.
- Installed then removed these external qBittorrent plugin files:
  `~/.local/share/qBittorrent/nova3/engines/rutracker.py` and
  `rutracker.png`. Restarting qBittorrent removes its loaded engine.
- Created this `AGENT_HANDOFF.md` only.

## Research findings

- Official Firefox add-on: https://addons.mozilla.org/en-US/firefox/addon/rutracker-add-on/
  Active v0.9.32 (March 2026); browser proxy-routing and interactive access.
  It does not expose a safe programmatic `httpx` path.
- `nbusseneau/qBittorrent-RuTracker-plugin`:
  https://github.com/nbusseneau/qBittorrent-RuTracker-plugin
  README declares deprecation since July 2026 because latest Cloudflare
  protection cannot be handled by this plugin.
- `RutrackerOrg/rutracker-proxy`:
  https://github.com/RutrackerOrg/rutracker-proxy
  Electron proxy application, not a solution to HTTP client fingerprint.
- Other GitHub HTTP parsers (e.g. `johnlepikhin/rutracker-api`,
  `idlesign/torrt`) are not evidence of a working current solution and likely
  hit the same access boundary.

## Stop conditions

Stop and report rather than implement if asked to:

- bypass Cloudflare, hide automation, spoof TLS/browser fingerprints, or
  automate challenge completion;
- store, display, commit, or transmit user credentials/cookies;
- change bootstrapping, lockfiles, dependencies, or external system state
  without a fresh plan and explicit `go`.

## Next action

Ask user for a new authorized goal. Reasonable directions:

1. Manual Firefox workflow plus a local/offline tool that processes already
   downloaded `.torrent` files.
2. Research an official/authorized RuTracker API or access method.
3. Change to a source that permits programmatic access.

No implementation is currently authorized or justified.
