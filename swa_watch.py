#!/usr/bin/env python3
"""Southwest fare watcher: daily search, filters, cash-vs-points value, Gmail alerts.

Usage:
  python swa_watch.py --login             # one-time: log in manually; session is saved
  python swa_watch.py                     # run searches and email alerts once
  python swa_watch.py --dry-run           # print results, no email, no dedup update
  python swa_watch.py --daily-at 07:20    # stay running, search once a day (Docker)
  python swa_watch.py --selftest          # launch the browser and exit (CI smoke)

State (config, browser profile, session, history DB, debug dumps) lives in
$SWA_DATA_DIR, default: the directory containing this script.
"""
import argparse
import datetime as dt
import json
import os
import random
import smtplib
import sqlite3
import sys
import time
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import urlencode

import yaml
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

BASE = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("SWA_DATA_DIR") or BASE).resolve()
PROFILE_DIR = DATA_DIR / ".swa_profile"
STATE_PATH = DATA_DIR / "storage_state.json"  # portable cookies, survives OS/browser changes
DB_PATH = DATA_DIR / "fares.db"
DEBUG_DIR = DATA_DIR / "debug"
SEARCH_URL = "https://www.southwest.com/air/booking/select-depart.html"
SHOPPING_API = "/air-booking/page/air/booking/shopping"
WEEKDAYS = {d: i for i, d in enumerate(["mon", "tue", "wed", "thu", "fri", "sat", "sun"])}


# ---------- helpers ----------
def num(v):
    if v is None:
        return None
    try:
        return float(str(v).replace(",", "").replace("$", ""))
    except ValueError:
        return None


def dump_debug(name, page=None, payload=None):
    DEBUG_DIR.mkdir(exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    if payload is not None:
        (DEBUG_DIR / f"{stamp}-{name}.json").write_text(json.dumps(payload, indent=2))
    if page is not None:
        try:
            page.screenshot(path=str(DEBUG_DIR / f"{stamp}-{name}.png"), full_page=True)
            (DEBUG_DIR / f"{stamp}-{name}.html").write_text(page.content())
        except Exception:
            pass


# ---------- parsing ----------
def lowest_fare(detail):
    """Lowest available fare across fare classes -> (total, taxes) or None."""
    best = None
    adult = (detail.get("fareProducts") or {}).get("ADULT") or {}
    for fp in adult.values():
        if not isinstance(fp, dict):
            continue
        if fp.get("availabilityStatus") not in (None, "AVAILABLE"):
            continue
        fare = fp.get("fare") or {}
        total = num((fare.get("totalFare") or {}).get("value"))
        if total is None:
            continue
        taxes = num((fare.get("totalTaxesAndFees") or {}).get("value")) or 0.0
        if best is None or total < best[0]:
            best = (total, taxes)
    return best


def parse_flights(payload):
    """Return {key: flight dict} or None if the payload shape is unrecognized."""
    try:
        products = payload["data"]["searchResults"]["airProducts"]
    except (KeyError, TypeError):
        return None
    out = {}
    for prod in products or []:
        for d in (prod or {}).get("details") or []:
            stops = d.get("numberOfStops")
            if stops is None:
                stops = max(len(d.get("stopsDetails") or [None]) - 1, 0)
            key = f'{d.get("flightNumbers")}|{d.get("departureDateTime") or d.get("departureTime")}'
            out[key] = {
                "flights": d.get("flightNumbers"),
                "depart": (d.get("departureTime") or "")[:5],
                "arrive": (d.get("arrivalTime") or "")[:5],
                "duration": int(num(d.get("totalDuration")) or 0),
                "stops": int(stops),
                "fare": lowest_fare(d),
            }
    return out


# ---------- browser ----------
def open_context(p, cfg, headless):
    b = cfg.get("browser") or {}
    kw = dict(
        user_data_dir=str(PROFILE_DIR),
        headless=headless,
        viewport={"width": 1366, "height": 900},
        locale="en-US",
    )
    channel = os.environ.get("SWA_BROWSER_CHANNEL", b.get("channel"))
    if channel:
        kw["channel"] = channel  # e.g. "chrome" for installed Google Chrome
    ctx = p.chromium.launch_persistent_context(**kw)
    if STATE_PATH.exists():
        try:
            ctx.add_cookies(json.loads(STATE_PATH.read_text()).get("cookies", []))
        except Exception as e:
            print(f"[warn] could not load {STATE_PATH.name}: {e}", file=sys.stderr)
    return ctx


def save_state(ctx):
    try:
        ctx.storage_state(path=str(STATE_PATH))
    except Exception as e:
        print(f"[warn] could not save {STATE_PATH.name}: {e}", file=sys.stderr)


def search(page, origin, dest, date, adults, fare_type, debug=False):
    q = {
        "adultPassengersCount": adults,
        "departureDate": date,
        "departureTimeOfDay": "ALL_DAY",
        "destinationAirportCode": dest,
        "fareType": fare_type,  # USD or POINTS
        "originationAirportCode": origin,
        "passengerType": "ADULT",
        "returnDate": "",
        "tripType": "oneway",
        "reset": "true",
    }
    tag = f"{origin}-{dest}-{date}-{fare_type}"
    try:
        with page.expect_response(
            lambda r: SHOPPING_API in r.url and r.request.method == "POST", timeout=60_000
        ) as info:
            page.goto(f"{SEARCH_URL}?{urlencode(q)}", wait_until="domcontentloaded")
        payload = info.value.json()
    except Exception as e:
        print(f"[warn] {tag}: no fare data ({e.__class__.__name__})", file=sys.stderr)
        dump_debug(tag, page=page)
        return None
    if debug:
        dump_debug(tag, payload=payload)
    flights = parse_flights(payload)
    if flights is None:
        print(f"[warn] {tag}: unrecognized response shape, saved to debug/", file=sys.stderr)
        dump_debug(tag, payload=payload)
    return flights


# ---------- logic ----------
def route_dates(rule):
    today = dt.date.today()
    if rule.get("dates"):
        ds = [dt.date.fromisoformat(str(d)) for d in rule["dates"]]
    else:
        a, b = rule.get("days_ahead", [1, 30])
        ds = [today + dt.timedelta(days=i) for i in range(a, b + 1)]
    if rule.get("weekdays"):
        keep = {WEEKDAYS[w[:3].lower()] for w in rule["weekdays"]}
        ds = [d for d in ds if d.weekday() in keep]
    return [d.isoformat() for d in ds if d >= today]


def combine(cash, points):
    cash, points = cash or {}, points or {}
    rows = []
    for k in set(cash) | set(points):
        base = cash.get(k) or points.get(k)
        c = (cash.get(k) or {}).get("fare")
        pf = (points.get(k) or {}).get("fare")
        r = {key: base[key] for key in ("flights", "depart", "arrive", "duration", "stops")}
        r["key"] = k
        r["cash"] = c[0] if c else None
        r["points"] = pf[0] if pf else None
        r["points_taxes"] = pf[1] if pf else None
        r["cpp"] = (
            round((r["cash"] - (r["points_taxes"] or 0)) / r["points"] * 100, 2)
            if r["cash"] and r["points"]
            else None
        )
        rows.append(r)
    return sorted(rows, key=lambda r: (r["cash"] or 1e9, r["points"] or 1e9))


def passes(r, rule):
    if r["stops"] > rule.get("max_stops", 99):
        return False
    if rule.get("max_duration_min") and r["duration"] > rule["max_duration_min"]:
        return False
    if rule.get("depart_after") and r["depart"] < rule["depart_after"]:
        return False
    if rule.get("depart_before") and r["depart"] > rule["depart_before"]:
        return False
    cash_ok = r["cash"] is not None and r["cash"] <= rule.get("max_price", float("inf"))
    pts_ok = (
        r["points"] is not None
        and r["points"] <= rule.get("max_points", float("inf"))
        and (r["cpp"] is None or r["cpp"] >= rule.get("min_cpp", 0))
    )
    return cash_ok or pts_ok


def recommend(r, rule):
    if r["cpp"] is None:
        return "Cash" if r["cash"] is not None else "Points"
    return "Points" if r["cpp"] >= rule.get("min_cpp", 1.3) else "Cash"


# ---------- storage ----------
def db():
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "CREATE TABLE IF NOT EXISTS history(ts TEXT, route TEXT, date TEXT, flights TEXT,"
        " depart TEXT, stops INT, duration INT, cash REAL, points REAL, cpp REAL)"
    )
    con.execute("CREATE TABLE IF NOT EXISTS alerted(key TEXT PRIMARY KEY, cash REAL, points REAL)")
    return con


def is_new_low(con, key, r):
    row = con.execute("SELECT cash, points FROM alerted WHERE key=?", (key,)).fetchone()
    if row is None:
        return True
    lc, lp = row
    return (r["cash"] is not None and (lc is None or r["cash"] < lc)) or (
        r["points"] is not None and (lp is None or r["points"] < lp)
    )


# ---------- email ----------
def fmt_dur(m):
    return f"{m // 60}h{m % 60:02d}"


def render(hits):
    head = "".join(
        f"<th style='text-align:left;padding:4px 8px'>{h}</th>"
        for h in ["Route", "Date", "Flights", "Dep–Arr", "Stops", "Duration", "Cash", "Points", "¢/pt", "Use"]
    )
    body = ""
    for h in hits:
        r = h["row"]
        pts = f'{int(r["points"]):,} + ${r["points_taxes"]:.2f}' if r["points"] else "—"
        cells = [
            f'<a href="{h["url"]}">{h["route"]}</a>', h["date"], r["flights"],
            f'{r["depart"]}–{r["arrive"]}', r["stops"], fmt_dur(r["duration"]),
            f'${r["cash"]:.0f}' if r["cash"] else "—", pts,
            f'{r["cpp"]:.2f}' if r["cpp"] else "—", h["use"],
        ]
        body += "<tr>" + "".join(f"<td style='padding:4px 8px'>{c}</td>" for c in cells) + "</tr>"
    return f"<table style='border-collapse:collapse;font-family:sans-serif;font-size:14px'><tr>{head}</tr>{body}</table>"


def send_email(to, subject, html):
    user, pw = os.environ["GMAIL_USER"], os.environ["GMAIL_APP_PASSWORD"]
    msg = MIMEText(html, "html")
    msg["Subject"], msg["From"], msg["To"] = subject, user, ", ".join(to)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(user, pw)
        s.sendmail(user, to, msg.as_string())


# ---------- entry points ----------
def do_login(cfg):
    if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
        sys.exit("--login needs a visible browser. Run it on a desktop, then copy "
                 f"{STATE_PATH.name} into the data directory.")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        ctx = open_context(p, cfg, headless=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://www.southwest.com/")
        input("Log in to Rapid Rewards in the browser window, then press Enter here... ")
        save_state(ctx)
        ctx.close()
    print("Session saved to", STATE_PATH)


def selftest():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content("<title>ok</title>")
        assert page.title() == "ok"
        browser.close()
    print("selftest ok")


def seconds_until(hhmm, now=None):
    now = now or dt.datetime.now()
    h, m = map(int, hhmm.split(":"))
    nxt = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if nxt <= now:
        nxt += dt.timedelta(days=1)
    return (nxt - now).total_seconds()


def daily(config_path, hhmm, debug=False):
    print(f"Scheduled daily at {hhmm} ({os.environ.get('TZ', 'local time')})", flush=True)
    while True:
        time.sleep(seconds_until(hhmm))
        try:
            run(load_config(config_path), debug=debug)
        except Exception as e:  # keep the service alive; next run tomorrow
            print(f"[error] run failed: {e!r}", file=sys.stderr, flush=True)


def load_config(path):
    return yaml.safe_load(Path(path).read_text())


def run(cfg, dry_run=False, debug=False):
    defaults = cfg.get("defaults") or {}
    b = cfg.get("browser") or {}
    delay = b.get("delay_sec", [8, 20])
    con = db()
    now = dt.datetime.now().isoformat(timespec="seconds")
    hits, attempts, failures = [], 0, 0

    with sync_playwright() as p:
        ctx = open_context(p, cfg, headless=b.get("headless", False))
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        for route in cfg["routes"]:
            rule = {**defaults, **route}
            o, d = rule["origin"].upper(), rule["destination"].upper()
            for date in route_dates(rule):
                results = {}
                for ft in ("USD", "POINTS"):
                    attempts += 1
                    results[ft] = search(page, o, d, date, rule.get("adults", 1), ft, debug)
                    failures += results[ft] is None
                    time.sleep(random.uniform(*delay))
                for r in combine(results["USD"], results["POINTS"]):
                    con.execute(
                        "INSERT INTO history VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (now, f"{o}-{d}", date, r["flights"], r["depart"], r["stops"],
                         r["duration"], r["cash"], r["points"], r["cpp"]),
                    )
                    if not passes(r, rule):
                        continue
                    key = f"{o}-{d}|{date}|{r['key']}"
                    if dry_run or is_new_low(con, key, r):
                        hits.append({
                            "key": key, "route": f"{o}→{d}", "date": date, "row": r,
                            "use": recommend(r, rule),
                            "url": f"{SEARCH_URL}?" + urlencode({
                                "originationAirportCode": o, "destinationAirportCode": d,
                                "departureDate": date, "tripType": "oneway",
                                "adultPassengersCount": rule.get("adults", 1)}),
                        })
                con.commit()
        save_state(ctx)
        ctx.close()

    to = (cfg.get("email") or {}).get("to") or [os.environ.get("GMAIL_USER")]
    if dry_run:
        for h in hits:
            r = h["row"]
            print(f'{h["route"]} {h["date"]} {r["flights"]} {r["depart"]}-{r["arrive"]} '
                  f'stops={r["stops"]} {fmt_dur(r["duration"])} cash={r["cash"]} '
                  f'pts={r["points"]} cpp={r["cpp"]} use={h["use"]}')
        print(f"{len(hits)} matches, {failures}/{attempts} searches failed")
        return

    if attempts and failures == attempts and (cfg.get("email") or {}).get("on_error", True):
        send_email(to, "SWA watcher: all searches failed",
                   "<p>No fare data returned. Likely blocked or session expired. "
                   "Run <code>python swa_watch.py --login</code> and check <code>debug/</code>.</p>")
    if hits:
        send_email(to, f"SWA fare alert: {len(hits)} match(es)", render(hits))
        for h in hits:
            r = h["row"]
            con.execute("INSERT OR REPLACE INTO alerted VALUES (?,?,?)", (h["key"], r["cash"], r["points"]))
        con.commit()
    print(f"{now}: {len(hits)} alerts, {failures}/{attempts} searches failed")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.environ.get("SWA_CONFIG") or str(DATA_DIR / "config.yaml"))
    ap.add_argument("--login", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--debug", action="store_true", help="save raw API JSON to debug/")
    ap.add_argument("--daily-at", metavar="HH:MM", default=os.environ.get("SWA_DAILY_AT"),
                    help="stay running and search once a day at this local time")
    ap.add_argument("--selftest", action="store_true", help="launch the browser and exit")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    for env in {BASE / ".env", DATA_DIR / ".env"}:
        load_dotenv(env)
    if not Path(a.config).exists():
        example = BASE / "config.example.yaml"
        if example.exists() and Path(a.config).parent == DATA_DIR:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            Path(a.config).write_text(example.read_text())
            sys.exit(f"Created {a.config} from the example. Edit it, then start again.")
        sys.exit(f"Config not found: {a.config}")
    if a.login:
        return do_login(load_config(a.config))
    if a.daily_at:
        return daily(a.config, a.daily_at, a.debug)
    run(load_config(a.config), a.dry_run, a.debug)


if __name__ == "__main__":
    main()
