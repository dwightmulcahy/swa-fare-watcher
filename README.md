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

One container runs the web app, a single background worker and a daily scheduler. At `SWA_DAILY_AT` every eligible user is queued; the worker processes one user at a time in one Chromium browser (headed inside a private Xvfb display, which Southwest blocks less often), with a random 8–20 s pause between page loads. A flight matches when it passes the stops, duration and time filters **and** either cash ≤ max cash, or points ≤ max points with ¢/pt ≥ the minimum. ¢/pt = (cash fare − points taxes) ÷ points × 100. Each user gets one email per run listing only flights that are new or cheaper than last alerted. If all of a user's searches fail, they get an email asking them to upload a fresh session.

State lives in `/data`: `app.db` (SQLite), `secret.key` (encrypts Southwest sessions; back it up with the DB) and `debug/` (screenshots and raw JSON of failed searches).

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

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | `admin` / random | First admin, created on first start only |
| `BASE_URL` | `http://localhost:8080` | Used in email links |
| `COOKIE_SECURE` | `false` | Set `true` behind HTTPS |
| `SECRET_KEY` | auto (`/data/secret.key`) | Encrypts stored Southwest sessions; keep stable |
| `GMAIL_USER`, `GMAIL_APP_PASSWORD` | — | Alert sender ([app password](https://myaccount.google.com/apppasswords), needs 2FA) |
| `SWA_DAILY_AT`, `TZ` | `07:20`, `UTC` | Daily run time |
| `SWA_SCHEDULER` | `true` | `false` disables the daily run (manual runs still work) |
| `DEFAULT_MAX_WATCHES`, `DEFAULT_MAX_SEARCHES_PER_DAY` | `5`, `40` | Pre-filled limits for new users |
| `MAX_DATES_PER_WATCH` | `31` | Hard cap on dates per watch |
| `SWA_SEARCH_DELAY` | `8,20` | Random pause range between searches (seconds) |
| `SWA_HEADLESS` | `false` | `true` runs Chromium headless (no Xvfb; more likely blocked) |
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
