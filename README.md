# SWA Fare Watcher

[![CI](https://github.com/dwightmulcahy/swa-fare-watcher/actions/workflows/ci.yml/badge.svg)](https://github.com/dwightmulcahy/swa-fare-watcher/actions/workflows/ci.yml)
[![Release](https://github.com/dwightmulcahy/swa-fare-watcher/actions/workflows/release.yml/badge.svg)](https://github.com/dwightmulcahy/swa-fare-watcher/actions/workflows/release.yml)
[![GitHub release](https://img.shields.io/github/v/release/dwightmulcahy/swa-fare-watcher?sort=semver)](https://github.com/dwightmulcahy/swa-fare-watcher/releases/latest)
[![Dependabot](https://img.shields.io/badge/dependabot-enabled-025e8c?logo=dependabot)](.github/dependabot.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Searches Southwest fares once a day for the routes you configure, filters by stops, duration, departure time, cash price and points, scores points fares in cents per point, and emails new or lower matches through Gmail.

> [!WARNING]
> Southwest has no public fare API. Fares are read from the JSON its booking page loads, which can change without notice, and automated access may conflict with Southwest's terms of use. Keep the number of searches small.

## How it works

- Each date is searched twice in a real Chromium browser: once in dollars, once in points. Requests per run = dates × 2.
- A flight matches when it passes the stops, duration and departure-time filters **and** either cash ≤ `max_price`, or points ≤ `max_points` with ¢/pt ≥ `min_cpp`.
- ¢/pt = (cash fare − points taxes) ÷ points × 100. The email's **Use** column says Points when ¢/pt ≥ `min_cpp`, else Cash.
- A flight is re-sent only when its cash or points price drops below the last alerted price.
- Every result is stored in `fares.db` (`history` table) for price trends.
- If every search fails (blocked or signed out), it emails you instead of failing silently.

## Configure

All state lives in the data directory (`$SWA_DATA_DIR`; the repo folder when running natively, `/data` in Docker):

| File | Purpose |
|---|---|
| `config.yaml` | Routes, filters, email recipients. Created from [`config.example.yaml`](config.example.yaml) on first start. |
| `.env` | `GMAIL_USER`, `GMAIL_APP_PASSWORD` (app password from myaccount.google.com/apppasswords; needs 2FA) |
| `storage_state.json` | Your Southwest session cookies, written by `--login`. Optional: searches work signed out. |
| `fares.db`, `debug/` | Price history; screenshots and raw JSON when a search fails |

Route options (each can also go under `defaults`): `origin`, `destination`, `dates` or `days_ahead: [from, to]`, `weekdays`, `adults`, `max_stops`, `max_duration_min`, `depart_after`, `depart_before`, `max_price`, `max_points`, `min_cpp`.

## Run natively

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
playwright install chromium        # skip if config uses browser.channel: chrome
cp .env.example .env               # set Gmail values
cp config.example.yaml config.yaml # set routes
python swa_watch.py --login        # optional: sign in once in the window that opens
python swa_watch.py --dry-run      # check parsing and filters, no email
```

Daily with cron (`xvfb-run -a` in front on a headless Linux server):

```cron
20 7 * * * cd /path/to/swa-fare-watcher && .venv/bin/python swa_watch.py >> watch.log 2>&1
```

## Run in Docker

The image runs Chromium headed inside Xvfb and searches once a day at `SWA_DAILY_AT` in `TZ`.

```bash
cp .env.example .env               # Gmail values, SWA_DAILY_AT, TZ
mkdir -p data && cp config.example.yaml data/config.yaml && $EDITOR data/config.yaml
sudo chown -R 1000 data            # container runs as uid 1000
docker compose run --rm --build watcher --dry-run   # test now
docker compose up -d
```

To use your Southwest login in the container, run `python swa_watch.py --login` on a desktop and copy the resulting `storage_state.json` into `data/`.

QNAP: `docker compose -f compose.qnap.yaml pull && docker compose -f compose.qnap.yaml up -d` (uses the published image and `QNAP_DATA_DIR`).

Both Compose files run with a read-only root filesystem, dropped capabilities, `no-new-privileges`, bounded memory and PIDs, an init process and rotated logs.

## When it breaks

- No results or `unrecognized response shape`: run `--dry-run --debug`, inspect `debug/*.json`, adjust `parse_flights()` / `lowest_fare()` in `swa_watch.py`.
- "All searches failed" email: likely blocked or the session expired. Run `--login` again and check `debug/*.png`.

## Development

```bash
pip install -r requirements-dev.txt
ruff check . --select F && pyflakes swa_watch.py scripts tests
pytest -q
```

CI runs the tests, validates both Compose files, builds the image, launches Chromium inside the hardened container, and fails on fixable high or critical vulnerabilities (Trivy). Actions and the base image are pinned by SHA/digest; Dependabot keeps them current.

## Releasing

1. Merge to `main`.
2. `git tag v1.2.3 && git push origin v1.2.3`
3. The Release workflow checks the tag is on `main`, reruns CI, publishes a multi-arch image (`ghcr.io/dwightmulcahy/swa-fare-watcher` by default, or Docker Hub when `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN` are set) and creates a GitHub Release with Conventional Commit notes.

The newest stable release also gets `1.2`, `1` and `latest` tags; prereleases (`v1.3.0-rc.1`) publish only their exact tag.

## License

[MIT](LICENSE)
