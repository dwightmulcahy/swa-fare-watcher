# SWA Fare Watcher

[![CI](https://github.com/dwightmulcahy/swa-fare-watcher/actions/workflows/ci.yml/badge.svg)](https://github.com/dwightmulcahy/swa-fare-watcher/actions/workflows/ci.yml)
[![Release](https://github.com/dwightmulcahy/swa-fare-watcher/actions/workflows/release.yml/badge.svg)](https://github.com/dwightmulcahy/swa-fare-watcher/actions/workflows/release.yml)
[![GitHub release](https://img.shields.io/github/v/release/dwightmulcahy/swa-fare-watcher?sort=semver)](https://github.com/dwightmulcahy/swa-fare-watcher/releases/latest)
[![Dependabot](https://img.shields.io/badge/dependabot-enabled-025e8c?logo=dependabot)](.github/dependabot.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A small multi-user web app that searches Southwest fares once a day for each user's saved watches, filters by stops, duration, departure time, cash price and points, scores points fares in cents per point, and emails new or lower matches through Gmail. An admin panel creates and revokes users and sets per-user limits.

> [!WARNING]
> Southwest has no public fare API. Fares are read from the JSON its booking page loads, which can change without notice, and automated access may conflict with Southwest's terms of use. Keep the number of users and searches small.

## Features

**Users**
- Sign in with a username and password; a temporary password from the admin must be changed at first sign-in.
- Create, edit, pause and delete watches: route, specific dates or a rolling window (e.g. next 14–45 days, Fridays and Saturdays), adults, max stops, max duration, departure window, max cash, max points, minimum ¢/point.
- See the latest fares per date (matches highlighted, with a Cash/Points recommendation), price history and recent runs; trigger **Search now**.
- Upload their own Southwest session so searches run as their Rapid Rewards account. Stored encrypted.

**Admin**
- Create users (temporary password shown once), reset passwords, revoke and restore access, delete users.
- Per-user limits: **max active watches**, **max searches per day**, **account expiry date**. Blank means no limit.
- See each user's status, usage today, Southwest session health and last sign-in; run everyone's watches now.

**How limits work**
- Each date in a watch costs 2 searches (cash + points). Creating or resuming a watch is refused if the user's active watches would need more searches per day than their limit.
- The worker also stops a user's run when the daily count is reached ("Daily search limit reached"), which covers **Search now**.
- Revoked or expired users are signed out immediately, can't sign in, and their watches don't run.

## How it works

One container runs the web app, a single background worker and a daily scheduler. At a random time around `SWA_DAILY_AT` (a new offset within ±`SWA_DAILY_JITTER_MINUTES` each day) every eligible user is queued in a shuffled order; the worker processes one user at a time in one browser, with a random 8–20 s pause between page loads. A flight matches when it passes the stops, duration and time filters **and** either cash ≤ max cash, or points ≤ max points with ¢/pt ≥ the minimum. ¢/pt = (cash fare − points taxes) ÷ points × 100. Each user gets one email per run listing only flights that are new or cheaper than last alerted. If all of a user's searches fail, they get an email asking them to upload a fresh session.

### Looking like a person, not a bot

Southwest's bot detection looks at the browser fingerprint and at how the page is used. The watcher:

- **Runs real Google Chrome** (amd64 image; arm64 falls back to Chromium, as Chrome has no Linux arm64 build), **headed** in a private Xvfb display, never headless.
- **Hides automation:** launches without `--enable-automation` and with `AutomationControlled` disabled, so `navigator.webdriver` is `false` natively (no injected script that could itself be detected).
- **Keeps a consistent fingerprint per user:** a persistent Chrome profile in `/data/profiles/<user id>` (Southwest's device cookies, cache and storage survive between runs, so it's the same "returning device" and less likely to trigger verification), a fixed real window size per user, real `en-US` language and the container's time zone (`TZ`, or `SWA_TIMEZONE`).
- **Behaves like a visitor** (`app/human.py`): lands on the home page first and looks around; after each result page loads, moves the mouse along curved, eased paths with tremor and occasional overshoot, scrolls with uneven wheel notches, and dwells a few seconds. Clicks land at a random point inside the target after a short hover, with a realistic press duration. Typing uses varied (log-normal) key gaps, slower at word boundaries, occasional hesitations, and neighbouring-key typos fixed with Backspace (off for passwords). All input goes through real trusted mouse/keyboard events.

`python -m app selftest` launches the browser exactly as searches do and checks the result: Chrome in use, `navigator.webdriver` false, no "Headless" in the user agent, all events trusted, and text typed with typos ends up exact. CI runs it inside the built image.

None of this guarantees Southwest won't block requests; it lowers the odds. Fewer searches per day help more than anything.

State lives in `/data`: `app.db` (SQLite), `secret.key` (encrypts Southwest sessions; back it up with the DB), `profiles/` (one Chrome profile per user; removed when the user clears their Southwest session or is deleted) and `debug/` (screenshots and raw JSON of failed searches).

## Run with Docker

```bash
cp .env.example .env        # set ADMIN_PASSWORD, GMAIL_*, BASE_URL, TZ
mkdir -p data && sudo chown 1000 data
docker compose up -d --build
open http://localhost:8080   # sign in as admin
```

If `ADMIN_PASSWORD` is blank, a temporary admin password is printed once in `docker compose logs`.

Put it behind an HTTPS reverse proxy (Caddy, Traefik, nginx, Cloudflare Tunnel) before giving others access, then set `COOKIE_SECURE=true` and `BASE_URL=https://…`.

QNAP: `docker compose -f compose.qnap.yaml pull && docker compose -f compose.qnap.yaml up -d` (uses the published image and `QNAP_DATA_DIR`).

Both Compose files run with a read-only root filesystem, dropped capabilities, `no-new-privileges`, bounded memory and PIDs, an init process and rotated logs.

## Southwest sessions

Each user uploads their own signed-in session under **Settings → Southwest session**, either:

- **Cookie export:** sign in at southwest.com, export cookies as JSON with a browser extension such as Cookie-Editor, and paste it; or
- **Helper script:** `python -m app swa-login` on a desktop with this repo and Playwright installed opens a browser, you sign in, and it writes `storage_state.json` to upload.

Only `southwest.com` cookies are kept. Sessions are encrypted with a key derived from `SECRET_KEY` (or `/data/secret.key`). Refreshed cookies are saved after each successful run. Without a session, searches run signed out (prices still show, but points prices are not account-specific).

### Automatic sign-in (optional)

Instead of re-uploading a session when it expires, users can save their Rapid Rewards username and password under **Settings → Automatic sign-in**. Before each run the worker opens Southwest's home page in that user's own browser profile (this is also the warm-up visit):

- **Still signed in:** nothing more to do. Thanks to the persistent profile and **Remember me**, this should be the usual case.
- **Signed out:** it signs in with the same human-like flow as `login-test`, then searches as that user and saves the refreshed session.
- **Verification code:** the user gets an email and a banner in the app, enters the code on the **Verify** page within `SWA_CODE_WAIT_SECONDS` (10 minutes), and the waiting browser finishes signing in. If no code arrives, it stops cleanly and asks again on the next day's run. Codes are deleted once used.

Guard rails, so automation never locks an account:

- At most **one** automatic attempt per user per day; **Try sign-in now** in Settings allows up to three a day and runs no searches.
- A **rejected password turns it off immediately** and emails the user; **two other failures in a row** (blocked, page not recognised, timeout) turn it off too.
- Credentials are encrypted like sessions (key from `SECRET_KEY` / `/data/secret.key`) and never shown back; users should use a password unique to Southwest.
- `SWA_AUTO_LOGIN=false` turns the feature off for everyone. Each attempt's screenshots and logs are kept under `/data/debug/signin/`.

While a sign-in waits for a code, the worker holds that user's browser open, so other users' runs wait too (at most the code timeout).

## Test automated sign-in on your machine

`python -m app login-test` signs in to southwest.com with your credentials using exactly what the app uses: real Chrome with the hardened launch settings, a persistent profile, and human-like mouse, typing and reading behaviour. Run it on your own computer to see whether Southwest lets an automated sign-in through.

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
playwright install chrome        # or: playwright install chromium (Linux arm64)
cp .env.example .env             # set SWA_USERNAME and SWA_PASSWORD (or omit SWA_PASSWORD to be prompted)
python -m app login-test
```

A Chrome window opens and you can watch it:

1. Opens southwest.com and looks around for a few seconds.
2. Clicks **Log in**, types your username (with the occasional corrected typo) and password (no typos), and submits with the form's own button.
3. Reports one of: `SUCCESS`, `ALREADY_SIGNED_IN` (the saved profile still has a session), `MFA_REQUIRED`, `BAD_CREDENTIALS`, `BLOCKED`, `NOT_FOUND` or `TIMEOUT`. The exit code is 0, 2, 3, 4 or 5 respectively.

- **Verification code:** if Southwest asks for one, the script prompts for it in the terminal and types it in. If Southwest first asks *how* to send the code, pick in the window and press Enter in the terminal.
- **On success** it saves `storage_state.json`, which you can upload in the app under **Settings → Southwest session**.
- **Every step is recorded** in `data/login-test/<timestamp>/`: numbered screenshots, `steps.log`, and `network.log` (Southwest request paths and status codes only; no bodies, query strings or credentials). If it fails, those files show exactly where.
- **The profile persists** in `data/profiles/login-test`, so a second run is a returning device and may skip the verification code. `--fresh` starts over as a new device.

| Option | Effect |
|---|---|
| `--fresh` | Empty profile (new device) |
| `--server` | Hidden virtual display like the container (Linux, needs `xvfb`) |
| `--keep-open` | Leave the window open at the end |
| `--no-assist` / `--no-prompt` | Never pause for help / never ask for a code (unattended behaviour) |
| `--save-state FILE` | Where to save the session on success |

Don't run it over and over if it fails: repeated failed sign-ins can lock the account. Element detection uses several strategies per field (accessible labels and roles first, then CSS) because Southwest's markup isn't documented; it's tested against a local fake airline site (`tests/fake_swa/`) for success, saved-profile, verification-code, wrong-password and blocked flows.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | `admin` / random | First admin, created on first start only |
| `BASE_URL` | `http://localhost:8080` | Used in email links |
| `COOKIE_SECURE` | `false` | Set `true` behind HTTPS |
| `SECRET_KEY` | auto (`/data/secret.key`) | Encrypts stored Southwest sessions; keep stable |
| `GMAIL_USER`, `GMAIL_APP_PASSWORD` | — | Alert sender ([app password](https://myaccount.google.com/apppasswords), needs 2FA) |
| `SWA_DAILY_AT`, `TZ` | `07:20`, `UTC` | Daily run time |
| `SWA_DAILY_JITTER_MINUTES` | `45` | Each day starts at a random time within ± this of `SWA_DAILY_AT`; users run in a shuffled order |
| `SWA_SCHEDULER` | `true` | `false` disables the daily run (manual runs still work) |
| `DEFAULT_MAX_WATCHES`, `DEFAULT_MAX_SEARCHES_PER_DAY` | `5`, `40` | Pre-filled limits for new users |
| `MAX_DATES_PER_WATCH` | `31` | Hard cap on dates per watch |
| `SWA_SEARCH_DELAY` | `8,20` | Random pause range between searches (seconds) |
| `SWA_HEADLESS` | `false` | `true` runs the browser headless (no Xvfb; more likely blocked) |
| `SWA_BROWSER_CHANNEL` | `auto` | `auto` = Google Chrome if installed, else Chromium; or `chrome`, `chromium`, `msedge` |
| `SWA_HUMANIZE` | `true` | Home-page warm-up and mouse/scroll/dwell behaviour around searches |
| `SWA_TIMEZONE` | container `TZ` | Browser time zone; match where the server's IP is |
| `SWA_AUTO_LOGIN` | `true` | Allow users to turn on automatic sign-in |
| `SWA_CODE_WAIT_SECONDS` | `600` | How long a sign-in waits for a verification code |
| `SESSION_DAYS` | `14` | Web sign-in lifetime |

## Run without Docker

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt && playwright install --with-deps chromium
cp .env.example .env && set -a && . ./.env && set +a
SWA_DATA_DIR=./data python -m app serve
```

On a desktop, Chromium opens in a visible window; on a headless Linux server the app starts Xvfb itself (`apt install xvfb`).

## When searches fail

- Users see "Searches failed" on their dashboard and get an email; the usual fix is uploading a fresh session.
- `unrecognized response shape` in the logs means Southwest changed its page data: inspect `/data/debug/*.json` and adjust `parse_flights()` / `lowest_fare()` in `app/swa.py`.

## Development

```bash
pip install -r requirements-dev.txt
ruff check . --select F && pyflakes app scripts tests
pytest -q
```

Tests cover parsing, filters, ¢/pt, scheduling, the worker (with a fake browser), sign-in, password change, watch CRUD and ownership, limits, revoke/restore/expiry, CSRF, session encryption and login rate limiting.

CI runs the tests, validates both Compose files, builds the image, starts it hardened, checks `/health`, signs in to the admin panel, launches Chromium inside the container, and fails on fixable high or critical vulnerabilities (Trivy). Actions and the base image are pinned by SHA/digest; Dependabot keeps them current.

## Releasing

1. Merge to `main`.
2. `git tag v1.2.3 && git push origin v1.2.3`
3. The Release workflow checks the tag is on `main`, reruns CI, publishes a multi-arch image (`ghcr.io/dwightmulcahy/swa-fare-watcher` by default, or Docker Hub when `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN` are set) and creates a GitHub Release with Conventional Commit notes.

## License

[MIT](LICENSE)
