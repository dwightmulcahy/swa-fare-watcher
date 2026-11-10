"""Southwest search, parsing, filtering and cash-vs-points scoring."""
import datetime as dt
import hashlib
import json
import logging
import os
import re
import select
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlencode

from . import applog
from .config import settings

log = logging.getLogger(__name__)

# Bot-protection products recognisable by cookie name (diagnostics only: which signals matter
# depends on which one Southwest uses). F5 BIG-IP ASM / Advanced WAF sets TS<hex> cookies
# (TSPD_101 is its Bot Defense); Shape (F5 Distributed Cloud Bot Defense) sends per-site
# request headers like X-AbCd1234-a ... -z.
PROTECTION_COOKIES = [
    ("F5 BIG-IP ASM / Bot Defense", re.compile(r"^(TS[0-9a-f]{6,}|TSPD_\w+|BIGipServer\w*|f5avr\w*|f5_cspm|F5_ST)$")),
    ("F5 BIG-IP APM (access sessions)", re.compile(r"^(LastMRH_Session|MRHSession|F5_fullWT|F5_HT_shrinked)$")),
    ("Akamai Bot Manager", re.compile(r"^(_abck|bm_sz|bm_sv|bm_mi|ak_bmsc)$")),
    ("Imperva", re.compile(r"^(reese84|incap_ses_\w+|visid_incap_\w+|nlbi_\w+)$")),
    ("PerimeterX / HUMAN", re.compile(r"^(_px\w*|_pxhd|_pxvid)$")),
    ("DataDome", re.compile(r"^datadome$")),
    ("Kasada", re.compile(r"^(x-kpsdk-\w+|KP_UIDz\w*)$", re.I)),
]
SHAPE_HEADER = re.compile(r"^x-[a-z0-9]{8}-([a-z])$", re.I)


def _site_host():
    """southwest.com (or the test site's host): the site whose protection is being looked at."""
    from urllib.parse import urlsplit
    host = urlsplit(settings.home_url).hostname or "southwest.com"
    return host[4:] if host.startswith("www.") else host

SEARCH_URL = "https://www.southwest.com/air/booking/select-depart.html"
# The Low Fare Calendar's "Shop in: Dollars | Points | $ + Points" switch: buttons labelled
# aria-label="Select points currency" / "Select dollars currency", aria-pressed when chosen.
# (The flight results page has no such switch.)
FARE_TOGGLE = {
    "POINTS": [("css", 'button[aria-label="Select points currency"]'),
               ("role", "button", r"^\s*select points currency\s*$"),
               ("css", "[role=group][aria-label='Shop in'] button:text-is('Points')")],
    "USD": [("css", 'button[aria-label="Select dollars currency"]'),
            ("role", "button", r"^\s*select dollars currency\s*$"),
            ("css", "[role=group][aria-label='Shop in'] button:text-is('Dollars')")],
}
CALENDAR_URL = "https://www.southwest.com/air/low-fare-calendar/select-dates"
SHOPPING_API = "/air-booking/page/air/booking/shopping"
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def num(v):
    if v is None:
        return None
    try:
        return float(str(v).replace(",", "").replace("$", ""))
    except ValueError:
        return None


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


def _text(v):
    """Flatten a value from Southwest's JSON to text: flight numbers arrive as a list
    (["1234", "567"] for a connection), sometimes as a string or number."""
    if v is None:
        return ""
    if isinstance(v, (list, tuple)):
        return "/".join(_text(x) for x in v if x not in (None, ""))
    if isinstance(v, dict):
        return _text(v.get("number") or v.get("flightNumber") or v.get("value") or "")
    return str(v)


def _clock(v):
    """HH:MM from "08:15", "8:15", "08:15:00" or an ISO date-time like "2026-12-18T08:15"."""
    m = re.search(r"(\d{1,2}):(\d{2})", _text(v).split("T")[-1])
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m else ""


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
            flights = _text(d.get("flightNumbers"))
            key = f'{flights}|{_text(d.get("departureDateTime") or d.get("departureTime"))}'
            out[key] = {
                "flights": flights,
                "depart": _clock(d.get("departureTime") or d.get("departureDateTime")),
                "arrive": _clock(d.get("arrivalTime") or d.get("arrivalDateTime")),
                "duration": int(num(d.get("totalDuration")) or 0),
                "stops": int(stops),
                "fare": lowest_fare(d),
            }
    return out


# ---------- low-fare calendar ----------
_ISO_DAY = re.compile(r"^(\d{4}-\d{2}-\d{2})")
_PRICE_KEYS = ("lowest", "price", "fare", "amount", "points", "value")
_SKIP_KEYS = ("original", "strike", "before", "was", "regular", "tax", "fee")


def _amount(v, depth=0):
    """A price from a number, "1,234", "$481" or {"value": "481"}-style objects."""
    if isinstance(v, bool) or v is None or depth > 3:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        return num(v)
    if isinstance(v, dict):
        for k in ("value", "amount", "totalFare", "price", "points", "fare"):
            if k in v:
                a = _amount(v[k], depth + 1)
                if a is not None:
                    return a
    return None


def _api_errors(payloads):
    """", Southwest said VALIDATION__X" from failed API responses, for the log."""
    codes = []
    for _, p in payloads:
        if isinstance(p, dict) and p.get("success") is False:
            for e in ((p.get("notifications") or {}).get("fieldErrors") or []) + \
                     ((p.get("notifications") or {}).get("formErrors") or []):
                if isinstance(e, dict) and e.get("code"):
                    codes.append(e["code"])
    return f" (Southwest said: {', '.join(dict.fromkeys(codes))})" if codes else ""


def _parse_low_fare_days(payload):
    """Southwest's own format: data.searchResults[].lowFareCalendarDays[] =
    {"date": "2027-03-01", "fares": {"WGA": {"totalFare": {"value": "368.52"}}, "PLU": ...}}.
    A day's low is its cheapest fare class; {} fares = nothing available. None if not this format."""
    try:
        results = payload["data"]["searchResults"]
    except (KeyError, TypeError):
        return None
    out = None
    for res in results if isinstance(results, list) else []:
        days = res.get("lowFareCalendarDays") if isinstance(res, dict) else None
        if not isinstance(days, list):
            continue
        out = out or {}
        for d in days:
            if not isinstance(d, dict) or not _ISO_DAY.match(str(d.get("date", ""))):
                continue
            lows = [num(((f or {}).get("totalFare") or {}).get("value"))
                    for f in (d.get("fares") or {}).values() if isinstance(f, dict)]
            lows = [x for x in lows if x is not None and x > 0]
            day = d["date"][:10]
            low = min(lows) if lows else None
            out[day] = low if out.get(day) is None else (min(out[day], low) if low is not None else out[day])
    return out


def parse_calendar(payload):
    exact = _parse_low_fare_days(payload)
    if exact is not None:
        return exact if any(v is not None for v in exact.values()) else None
    return _parse_calendar_guess(payload)


def _parse_calendar_guess(payload):
    """{YYYY-MM-DD: lowest fare that day (None = unavailable)} from the calendar's JSON, or None
    if nothing in it looks like a day with a price. Southwest's format isn't documented, so this
    looks for any object that has a date and a price; struck-through "was" prices are ignored."""
    out = {}

    def walk(x, depth=0):
        if depth > 14:
            return
        if isinstance(x, dict):
            day = None
            for k, v in x.items():
                if "date" in k.lower() and isinstance(v, str) and _ISO_DAY.match(v):
                    day = v[:10]
                    break
            if day:
                amt, unavailable = None, False
                for k, v in x.items():
                    kl = k.lower()
                    if any(t in kl for t in _SKIP_KEYS):
                        continue
                    if any(t in kl for t in _PRICE_KEYS):
                        a = _amount(v)
                        if a is not None and a > 0:
                            amt = a if amt is None else min(amt, a)
                    if (kl in ("unavailable", "isunavailable", "soldout") and v is True) or (
                            isinstance(v, str) and v.strip().upper().replace(" ", "_") in ("UNAVAILABLE", "SOLD_OUT")):
                        unavailable = True
                if amt is not None:
                    out[day] = amt if out.get(day) is None else min(out[day], amt)
                elif unavailable:
                    out.setdefault(day, None)
            for v in x.values():
                walk(v, depth + 1)
        elif isinstance(x, list):
            for v in x:
                walk(v, depth + 1)

    walk(payload)
    return out if any(v is not None for v in out.values()) else None


# Responses never read or saved: account and sign-in data (personal details, tokens)
PRIVATE_API = ("/api/security/", "/userinfo", "/oauth", "/token", "/login", "/account", "/profile")
MIN_GUESSED_DAYS = 5   # a guessed calendar must price at least this many days
CALENDAR_WAIT = 45     # seconds to wait for the calendar's fare data after loading it


def is_private(url):
    path = url.split("?")[0].lower()
    return any(p in path for p in PRIVATE_API)


def best_calendar(payloads, final=True):
    """(url, payload, days, exact) for the best calendar among [(url, parsed JSON)], or None.
    Southwest's own format (lowFareCalendarDays) always wins. The format guesser is only a last
    resort (final=True, once nothing else is coming) and must find several priced days, so a
    stray response with one date and a number in it isn't taken for a calendar."""
    best = None
    for url, payload in payloads:
        if is_private(url):
            continue
        days = _parse_low_fare_days(payload)
        if days and any(v is not None for v in days.values()):
            n = sum(v is not None for v in days.values())
            if best is None or n > best[0]:
                best = (n, url, payload, days)
    if best:
        return (*best[1:], True)
    if not final:
        return None
    for url, payload in payloads:
        if is_private(url):
            continue
        days = _parse_calendar_guess(payload)
        n = sum(v is not None for v in (days or {}).values())
        if n >= MIN_GUESSED_DAYS and (best is None or n > best[0]):
            best = (n, url, payload, days)
    return (*best[1:], False) if best else None


_MONTHS = {m: i for i, m in enumerate(("january", "february", "march", "april", "may", "june", "july",
                                       "august", "september", "october", "november", "december"), 1)}
_CELL = re.compile(r"^\s*[A-Za-z]+day\s+(\d{1,2}),\s*(.+?)\s*$")

# What the Low Fare Calendar shows: the selected month tab ("December 2026") and a button per
# day labelled "Sunday 20, $418" / "Wednesday 2, Unavailable" (+ "  Selected depart date").
CALENDAR_DOM_JS = """() => {
  const tab = document.querySelector('[role=tab][aria-selected=true][aria-label]');
  const panel = tab && document.getElementById(tab.getAttribute('aria-controls')) || document;
  return { month: tab ? tab.getAttribute('aria-label') : null,
           cells: [...panel.querySelectorAll('[role=gridcell] button[aria-label]')].map(b => b.getAttribute('aria-label')) };
}"""


def parse_calendar_cells(month, cells):
    """{YYYY-MM-DD: amount or None} from the calendar's month label and day-cell labels, or None."""
    try:
        name, year = (month or "").strip().rsplit(" ", 1)
        m, y = _MONTHS[name.lower()], int(year)
    except (ValueError, KeyError):
        return None
    out = {}
    for label in cells or []:
        hit = _CELL.match(str(label))
        if not hit:
            continue
        try:
            day = dt.date(y, m, int(hit.group(1))).isoformat()
        except ValueError:
            continue
        rest = hit.group(2).split("  ")[0]          # drop "  Selected depart date"
        out[day] = None if "unavailable" in rest.lower() else num(rest)
    return out if any(v is not None for v in out.values()) else None


def calendar_months(dates):
    """For each month the dates fall in, the earliest of those dates (the calendar opens on it)."""
    first = {}
    for d in sorted(dates):
        first.setdefault(d[:7], d)
    return first


def _day_low(lows, d):
    x = (lows or {}).get(d) or {}
    return x.get("cash"), x.get("points")


def _rank(cash, points):
    # cheapest first; points-only days ranked at ~1.3¢/pt
    return cash if cash is not None else (points * 0.013 if points is not None else float("inf"))


def calendar_pick(w, cal, legs, limit):
    """Which (leg, date) searches to run, given calendar lows.

    cal = {"out": {date: {"cash", "points"}}, "ret": {...}}, with a leg missing (None) when its
    calendar couldn't be read: that leg keeps every date. A day's calendar low is the cheapest
    fare that day, so a day whose low already misses the target can't produce a match and is
    skipped; filters (stops, times) only make fares dearer. All prices are per person.
    Round trips rank date pairs by the sum of the two lows. At most `limit` dates per leg
    (or pairs for a round trip) are kept, cheapest first."""
    by_leg = {"out": [], "ret": []}
    for leg, d in legs:
        by_leg[leg].append(d)

    def priced(leg, d):
        return cal.get(leg) is not None and d in cal[leg]

    if not is_roundtrip(w):
        if cal.get("out") is None:
            return list(legs)
        keep = []
        for d in by_leg["out"]:
            c, p = _day_low(cal["out"], d)
            if not priced("out", d):
                keep.append((float("inf"), d))      # not on the calendar: search to be sure
            elif (c is not None or p is not None) and price_ok(c, p, None, w):
                keep.append((_rank(c, p), d))
        keep.sort()
        return [("out", d) for _, d in keep[:limit]]

    pairs = [(o, r) for o, r in trip_pairs(w) if o in by_leg["out"] and r in by_leg["ret"]]
    scored = []
    for o, r in pairs:
        oc, op = _day_low(cal.get("out"), o) if priced("out", o) else (None, None)
        rc, rp = _day_low(cal.get("ret"), r) if priced("ret", r) else (None, None)
        unknown_o, unknown_r = not priced("out", o), not priced("ret", r)
        if (not unknown_o and oc is None and op is None) or (not unknown_r and rc is None and rp is None):
            continue                                 # sold out one way
        cash = None if (unknown_o or unknown_r or oc is None or rc is None) else oc + rc
        pts = None if (unknown_o or unknown_r or op is None or rp is None) else op + rp
        if unknown_o or unknown_r:
            scored.append((float("inf"), o, r))
        elif (cash is not None or pts is not None) and price_ok(cash, pts, None, w):
            scored.append((_rank(cash, pts), o, r))
    scored.sort()
    chosen = scored[:limit]
    outs = {o for _, o, _ in chosen}
    rets = {r for _, _, r in chosen}
    return [(leg, d) for leg, d in legs if (d in outs if leg == "out" else d in rets)]


def calendar_routes(w, cal_routes, picked):
    """Which airport pairs to fully search for each picked (leg, date).

    cal_routes = {leg: {(from, to): {date: {"cash", "points"}} or None (unreadable)}}.
    A pair is skipped on a day when its own calendar low can't make the target (for a round
    trip: even with the cheapest return/outbound low among the picked dates). Unknown lows
    are always searched. Returns {(leg, date): [(from, to), ...]}."""
    def low(leg, rt_, d):
        x = (cal_routes.get(leg) or {}).get(rt_)
        if x is None or d not in x:
            return None, None, True        # unknown
        v = x[d] or {}
        return v.get("cash"), v.get("points"), False

    rt = is_roundtrip(w)
    best = {}   # leg -> (cheapest cash, cheapest points) over picked dates and pairs, None = unknown
    if rt:
        for leg in ("out", "ret"):
            cs, ps, unknown = [], [], False
            for lg, d in picked:
                if lg != leg:
                    continue
                for rt_ in routes(w, leg):
                    c, p, u = low(leg, rt_, d)
                    unknown |= u
                    cs += [c] if c is not None else []
                    ps += [p] if p is not None else []
            best[leg] = None if unknown else (min(cs, default=None), min(ps, default=None))
    out = {}
    for leg, d in picked:
        keep = []
        for rt_ in routes(w, leg):
            c, p, unknown = low(leg, rt_, d)
            if unknown:
                keep.append(rt_)
                continue
            if c is None and p is None:
                continue                    # sold out on this pair
            if rt:
                other = best.get("ret" if leg == "out" else "out")
                if other is None:
                    keep.append(rt_)
                    continue
                oc, op = other
                c = c + oc if c is not None and oc is not None else None
                p = p + op if p is not None and op is not None else None
                if c is None and p is None:
                    continue
            if price_ok(c, p, None, w):
                keep.append(rt_)
        out[(leg, d)] = keep
    return out


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


# ---------- rules ----------
def _get(w, key, default=None):
    """Watch column, tolerating dicts/rows created before round trips existed."""
    try:
        v = w[key]
    except (KeyError, IndexError):
        return default
    return default if v is None else v


def is_roundtrip(w):
    return _get(w, "trip_type", "oneway") == "roundtrip"


def watch_dates(w, today=None):
    """Outbound dates a watch searches. `w` is a mapping with watch columns."""
    today = today or dt.date.today()
    if w["dates"]:
        ds = sorted({dt.date.fromisoformat(d.strip()) for d in w["dates"].split(",") if d.strip()})
    elif _get(w, "date_from") and _get(w, "date_to"):
        a = max(dt.date.fromisoformat(w["date_from"]), today)
        b = dt.date.fromisoformat(w["date_to"])
        ds = [a + dt.timedelta(days=i) for i in range((b - a).days + 1)]
    else:
        a, b = w["days_from"] or 0, w["days_to"] or 0
        ds = [today + dt.timedelta(days=i) for i in range(a, b + 1)]
    if w["weekdays"]:
        keep = {WEEKDAYS.index(x) for x in w["weekdays"].split(",") if x in WEEKDAYS}
        ds = [d for d in ds if d.weekday() in keep]
    return [d.isoformat() for d in ds if d >= today][: settings.max_dates_per_watch]


def return_dates(w, today=None):
    """Return-leg dates for a round trip: the listed return dates, or every outbound date plus
    each allowed stay length (nights_min..nights_max)."""
    if not is_roundtrip(w):
        return []
    today = today or dt.date.today()
    explicit = _get(w, "ret_dates", "")
    if explicit:
        ds = {dt.date.fromisoformat(d.strip()) for d in explicit.split(",") if d.strip()}
    else:
        lo, hi = _get(w, "nights_min", 0), _get(w, "nights_max", 0)
        ds = {dt.date.fromisoformat(o) + dt.timedelta(days=n)
              for o in watch_dates(w, today) for n in range(lo, hi + 1)}
    return [d.isoformat() for d in sorted(ds) if d >= today][: settings.max_dates_per_watch]


def trip_pairs(w, today=None):
    """(outbound date, return date) combinations a round trip considers."""
    outs, rets = watch_dates(w, today), return_dates(w, today)
    explicit = bool(_get(w, "ret_dates", ""))
    lo, hi = _get(w, "nights_min", 0), _get(w, "nights_max", 365)
    pairs = []
    for o in outs:
        for r in rets:
            nights = (dt.date.fromisoformat(r) - dt.date.fromisoformat(o)).days
            if nights >= 0 and (explicit or lo <= nights <= hi):
                pairs.append((o, r))
    return pairs


def search_legs(w, today=None):
    """Every (leg, date) a run searches; each costs 2 searches (cash + points)."""
    legs = [("out", d) for d in watch_dates(w, today)]
    if is_roundtrip(w):
        pair_dates = {r for _, r in trip_pairs(w, today)}
        legs += [("ret", d) for d in return_dates(w, today) if d in pair_dates]
    return legs


MAX_AIRPORTS = 3   # per side


def airports(s):
    """"SJO, LIR" / "SJO/LIR" -> ["SJO", "LIR"] (order kept, duplicates dropped)."""
    out = []
    for a in re.split(r"[,/\s]+", (s or "").upper()):
        if a and a not in out:
            out.append(a)
    return out


def routes(w, leg="out"):
    """(from, to) airport pairs searched for a leg: every origin × destination; the return
    leg is the same pairs reversed."""
    pairs = [(a, b) for a in airports(w["origin"]) for b in airports(w["destination"]) if a != b]
    return pairs if leg == "out" else [(b, a) for a, b in pairs]


def route_label(w, leg="out"):
    o, d = "/".join(airports(w["origin"])), "/".join(airports(w["destination"]))
    return f"{o}→{d}" if leg == "out" else f"{d}→{o}"


def searches_per_run(w, today=None) -> int:
    return 2 * sum(len(routes(w, leg)) for leg, _ in search_legs(w, today))


def passes_leg(r, w, leg="out"):
    """Flight filters (stops, duration, departure window) for one leg; no price check."""
    if w["max_stops"] is not None and r["stops"] > w["max_stops"]:
        return False
    if w["max_duration_min"] and r["duration"] > w["max_duration_min"]:
        return False
    after = w["depart_after"] if leg == "out" else _get(w, "ret_depart_after")
    before = w["depart_before"] if leg == "out" else _get(w, "ret_depart_before")
    if after and r["depart"] < after:
        return False
    if before and r["depart"] > before:
        return False
    return True


def best_only(w):
    """No price target: report just the cheapest fare found, whenever it drops."""
    return _get(w, "max_price") is None and _get(w, "max_points") is None


def cheapest_first(r):
    return (r["cash"] if r["cash"] is not None else float("inf"),
            r["points"] if r["points"] is not None else float("inf"))


def price_ok(cash, points, cpp, w):
    max_price = w["max_price"] if w["max_price"] is not None else float("inf")
    max_points = w["max_points"] if w["max_points"] is not None else float("inf")
    cash_ok = cash is not None and cash <= max_price
    pts_ok = points is not None and points <= max_points and (cpp is None or cpp >= (w["min_cpp"] or 0))
    return cash_ok or pts_ok


def passes(r, w, leg="out"):
    return passes_leg(r, w, leg) and price_ok(r["cash"], r["points"], r["cpp"], w)


def recommend(r, w):
    if r["cpp"] is None:
        return "Cash" if r["cash"] is not None else "Points"
    return "Points" if r["cpp"] >= (w["min_cpp"] or 0) else "Cash"


def _cpp(cash, points, taxes):
    return round((cash - (taxes or 0)) / points * 100, 2) if cash and points else None


def roundtrips(w, legs):
    """Best round trips from per-leg results. legs = {"out": {date: [flight rows]}, "ret": {...}}.

    Southwest prices each direction on its own, so a round trip costs exactly the sum of two
    one-ways: for each date pair the cheapest round trip is the cheapest eligible flight each way.
    With several airports, any outbound airport pair can combine with any return pair, unless
    same_airports is on (back from the airport you flew into, to the one you left from).
    Returns one dict per date pair, cheapest cash first:
      cash: total with the cheapest cash flight each way (out_cash / ret_cash rows)
      points, taxes, cpp: the same with the cheapest points flight each way
      mix: when mix_payment is on, the best payment per leg (points where that leg's ¢/pt
           clears the minimum, else cash), e.g. cash out and points back
      url / ret_url: booking links (one round-trip link, or one per direction when the
           airports differ)
    """
    out = []
    for o, r in trip_pairs(w):
        o_rows = [x for x in legs.get("out", {}).get(o, []) if passes_leg(x, w, "out")]
        r_rows = [x for x in legs.get("ret", {}).get(r, []) if passes_leg(x, w, "ret")]
        if not o_rows or not r_rows:
            continue
        if _get(w, "same_airports", 0):
            groups = {}
            for x in o_rows:
                groups.setdefault((x.get("origin"), x.get("dest")), []).append(x)
            cands = [_price_pair(w, o, r, g, [y for y in r_rows if (y.get("origin"), y.get("dest")) == (b, a)])
                     for (a, b), g in groups.items()]
            cands = [c for c in cands if c]
            if not cands:
                continue
            trip = min(cands, key=_trip_order)
        else:
            trip = _price_pair(w, o, r, o_rows, r_rows)
        out.append(trip)
    return sorted(out, key=_trip_order)


def _trip_order(t):
    return (t["cash"] if t["cash"] is not None else 1e9, t["points"] if t["points"] is not None else 1e9)


def _price_pair(w, o, r, o_rows, r_rows):
    if not o_rows or not r_rows:
        return None
    min_cpp = w["min_cpp"] or 0

    def cheapest(rows, field):
        rows = [x for x in rows if x[field] is not None]
        return min(rows, key=lambda x: x[field]) if rows else None

    oc, rc = cheapest(o_rows, "cash"), cheapest(r_rows, "cash")
    op, rp = cheapest(o_rows, "points"), cheapest(r_rows, "points")
    cash = oc["cash"] + rc["cash"] if oc and rc else None
    points = op["points"] + rp["points"] if op and rp else None
    taxes = (op["points_taxes"] or 0) + (rp["points_taxes"] or 0) if op and rp else None
    trip = {
        "out_date": o, "ret_date": r,
        "nights": (dt.date.fromisoformat(r) - dt.date.fromisoformat(o)).days,
        "out_cash": oc, "ret_cash": rc, "out_points": op, "ret_points": rp,
        "cash": cash, "points": points, "taxes": taxes, "cpp": _cpp(cash, points, taxes),
    }
    mix = []
    for leg, c, pt in (("out", oc, op), ("ret", rc, rp)):
        leg_cpp = _cpp(c["cash"], pt["points"], pt["points_taxes"]) if c and pt else None
        use_points = pt is not None and (c is None or (leg_cpp is not None and leg_cpp >= min_cpp))
        mix.append({"leg": leg, "pay": "points" if use_points else "cash",
                    "flight": pt if use_points else c, "cpp": leg_cpp})
    trip["mix"] = mix
    trip["mix_cash"] = sum(m["flight"]["cash"] for m in mix if m["pay"] == "cash" and m["flight"])
    trip["mix_points"] = sum(m["flight"]["points"] for m in mix if m["pay"] == "points" and m["flight"])
    trip["mix_taxes"] = sum(m["flight"]["points_taxes"] or 0 for m in mix if m["pay"] == "points" and m["flight"])
    trip["use"] = use_label(trip, w)
    trip["matched"] = price_ok(cash, points, trip["cpp"], w)
    first_o = trip["out_cash"] or trip["out_points"]
    first_r = trip["ret_cash"] or trip["ret_points"]
    fo, to_ = (first_o.get("origin"), first_o.get("dest")) if first_o else (None, None)
    fr, tr = (first_r.get("origin"), first_r.get("dest")) if first_r else (None, None)
    fo = fo or (airports(_get(w, "origin", "")) + ["?"])[0]
    to_ = to_ or (airports(_get(w, "destination", "")) + ["?"])[0]
    fr, tr = fr or to_, tr or fo
    adults = _get(w, "adults", 1)
    trip["out_route"], trip["ret_route"] = f"{fo}→{to_}", f"{fr}→{tr}"
    if (fr, tr) == (to_, fo):
        trip["url"], trip["ret_url"] = booking_url(fo, to_, o, adults, r), None
    else:
        trip["url"], trip["ret_url"] = booking_url(fo, to_, o, adults), booking_url(fr, tr, r, adults)
    return trip


def use_label(trip, w):
    """How to pay: whole trip in cash or points, or, with mix_payment, per leg."""
    if _get(w, "mix_payment", 1):
        pays = [m["pay"] for m in trip["mix"]]
        if pays[0] != pays[1]:
            return f"{pays[0].capitalize()} out, {pays[1]} back"
        return pays[0].capitalize()
    if trip["cpp"] is None:
        return "Cash" if trip["cash"] is not None else "Points"
    return "Points" if trip["cpp"] >= (w["min_cpp"] or 0) else "Cash"


def search_query(origin, dest, date, adults=1, fare_type="USD", return_date=None):
    """The select-depart query Southwest's own search form sends. The page rejects requests
    missing any of these ("Sorry, we found some errors..."), so links and searches share it."""
    return {
        "adultPassengersCount": adults, "adultsCount": adults,
        "departureDate": date, "departureTimeOfDay": "ALL_DAY",
        "destinationAirportCode": dest, "fareType": fare_type,
        "lapInfantPassengersCount": 0, "originationAirportCode": origin,
        "passengerType": "ADULT", "promoCode": "",
        "returnDate": return_date or "", "returnTimeOfDay": "ALL_DAY",
        "tripType": "roundtrip" if return_date else "oneway", "reset": "true",
    }


def booking_url(origin, dest, date, adults=1, return_date=None, fare_type="USD"):
    return f"{SEARCH_URL}?" + urlencode(search_query(origin, dest, date, adults, fare_type, return_date))


# ---------- Southwest session import ----------
_SAMESITE = {"no_restriction": "None", "none": "None", "lax": "Lax", "strict": "Strict",
             "unspecified": "Lax"}


def normalize_state(raw: str) -> dict:
    """Accept a Playwright storage_state.json or a browser-extension cookie export
    (JSON array). Returns a Playwright storage_state dict with southwest.com cookies."""
    data = json.loads(raw)
    cookies = data.get("cookies") if isinstance(data, dict) else data
    if not isinstance(cookies, list):
        raise ValueError("Expected a storage_state.json or a JSON array of cookies.")
    out = []
    for c in cookies:
        if not isinstance(c, dict) or "southwest.com" not in str(c.get("domain", "")):
            continue
        exp = c.get("expires", c.get("expirationDate", -1))
        if c.get("session") or exp is None:
            exp = -1
        out.append({
            "name": str(c["name"]), "value": str(c.get("value", "")),
            "domain": c["domain"], "path": c.get("path") or "/",
            "expires": float(exp), "httpOnly": bool(c.get("httpOnly")),
            "secure": bool(c.get("secure")),
            "sameSite": _SAMESITE.get(str(c.get("sameSite", "Lax")).lower(), "Lax"),
        })
    if not out:
        raise ValueError("No southwest.com cookies found in that file.")
    origins = [o for o in (data.get("origins", []) if isinstance(data, dict) else [])
               if "southwest.com" in o.get("origin", "")]
    return {"cookies": out, "origins": origins}


# ---------- browser ----------
HOME_URL = "https://www.southwest.com/"
# Common real desktop window sizes; each user always gets the same one (stable fingerprint).
WINDOW_SIZES = [(1366, 768), (1440, 900), (1536, 864), (1600, 900), (1680, 1050), (1920, 1080), (1280, 800)]
SCREEN = (1920, 1080)
LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",  # navigator.webdriver stays false, like a real browser
    "--no-first-run",
    "--no-default-browser-check",
    "--password-store=basic",
    "--lang=en-US",
    "--disk-cache-size=67108864",
    # Official Chrome shows an "unsupported command-line flag" warning bar for --no-sandbox and
    # --disable-blink-features; the bar makes innerHeight ~55 px smaller than a normal window,
    # which a page can measure. --test-type suppresses the bar.
    "--test-type",
]
# Playwright adds these by default; real Chrome never runs with them, and each one changes
# something a page can observe (automation flag, popup blocking, back/forward cache, colour
# management). Playwright's --disable-field-trial-config stays: official Chrome doesn't ship the
# testing field-trial config it disables, while Chromium / Chrome for Testing would otherwise turn
# on experimental features (e.g. trimmed navigator.languages) that real users don't have.
IGNORE_DEFAULT_ARGS = [
    "--enable-automation",
    "--disable-component-extensions-with-background-pages",
    "--disable-popup-blocking",
    "--disable-back-forward-cache",
    "--force-color-profile=srgb",
]


def window_size_for(key):
    if key is None:
        return WINDOW_SIZES[0]
    h = int(hashlib.sha256(f"window:{key}".encode()).hexdigest(), 16)
    return WINDOW_SIZES[h % len(WINDOW_SIZES)]


def profile_dir(key):
    return settings.data_dir / "profiles" / str(key)


def wipe_profile(key):
    shutil.rmtree(profile_dir(key), ignore_errors=True)


def start_xvfb(timeout=10, attempts=3):
    """Start a private virtual display so Chrome can run headed without a desktop.
    Returns (process, ':N'). Retries: right after another Xvfb exits, a new one can pick the
    display number still being released and quit straight away."""
    if not shutil.which("Xvfb"):
        raise RuntimeError("Xvfb not installed: install it or set SWA_HEADLESS=true")
    for attempt in range(1, attempts + 1):
        try:
            return _start_xvfb_once(timeout)
        except RuntimeError as e:
            if attempt == attempts:
                raise
            log.warning("%s; retrying (%d/%d)", e, attempt, attempts - 1)
            time.sleep(0.5 * attempt)


def _start_xvfb_once(timeout):
    r, w = os.pipe()
    proc = subprocess.Popen(
        ["Xvfb", "-displayfd", str(w), "-screen", "0", f"{SCREEN[0]}x{SCREEN[1]}x24", "-nolisten", "tcp"],
        pass_fds=(w,), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    os.close(w)
    try:
        ready, _, _ = select.select([r], [], [], timeout)
        num = os.read(r, 32).decode().strip() if ready else ""
    finally:
        os.close(r)
    if not num.isdigit():
        proc.kill()
        raise RuntimeError(f"Xvfb did not start{_xvfb_err(proc)}")
    # make sure it's really accepting connections (a display number just released by another
    # Xvfb can briefly look free while its socket is still going away)
    sock = Path(f"/tmp/.X11-unix/X{num}")
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and proc.poll() is None and not sock.exists():
        time.sleep(0.05)
    if proc.poll() is not None:
        raise RuntimeError(f"Xvfb exited right after starting on :{num}{_xvfb_err(proc)}")
    proc.stderr.close()   # running: nothing more to read, and an unread pipe could fill up
    return proc, f":{num}"


def _xvfb_err(proc):
    try:
        proc.wait(timeout=2)
        err = proc.stderr.read().decode(errors="replace").strip().splitlines()
        proc.stderr.close()
        return f" ({err[-1][:200]})" if err else ""
    except Exception:
        return ""


class BrowserSearcher:
    """One persistent browser profile per user. The profile keeps Southwest's device cookies,
    cache and local storage between runs, so the site sees the same returning browser."""

    _xvfb = None

    def __init__(self, state=None, profile_key=None, human=None):
        self.state = state
        self.profile_key = profile_key
        self.humanize = settings.humanize if human is None else human
        self._pw = self._ctx = self.page = self.human = None
        self._tmp_profile = None
        self._warmed = False
        self.channel = None

    def __enter__(self):
        from playwright.sync_api import sync_playwright

        from .human import Human

        width, height = window_size_for(self.profile_key)
        env = {**os.environ, "LANG": "en_US.UTF-8"}
        if not settings.headless and not os.environ.get("DISPLAY") and sys.platform.startswith("linux"):
            self._xvfb, display = start_xvfb()
            env["DISPLAY"] = display
        if self.profile_key is None:
            self._tmp_profile = tempfile.mkdtemp(prefix="swa-profile-")
            user_dir = self._tmp_profile
        else:
            user_dir = profile_dir(self.profile_key)
            user_dir.mkdir(parents=True, exist_ok=True)
        kw = dict(
            user_data_dir=str(user_dir), headless=settings.headless, env=env,
            args=[*LAUNCH_ARGS, f"--window-size={width},{height}", "--window-position=0,0"],
            ignore_default_args=IGNORE_DEFAULT_ARGS,
            no_viewport=True,  # page size comes from the real window, so inner/outer/screen sizes agree
            # No Playwright `locale`: its override trims languages to ["en-US"] / "en-US". With just
            # --lang, Chrome reports ["en-US","en"] and "en-US,en;q=0.9" like a normal install.
        )
        if settings.browser_timezone:
            kw["timezone_id"] = settings.browser_timezone
        try:
            self._pw = sync_playwright().start()
            self._ctx = self._launch(kw)
            if self.state and self.state.get("cookies"):
                self._ctx.add_cookies(self.state["cookies"])
            self.page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
            self.page.set_default_timeout(30_000)
            self.human = Human(self.page)
            self._protection_headers = set()
            self._ctx.on("request", self._note_request)
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def _launch(self, kw):
        want = settings.browser_channel
        if want in ("auto", "chrome"):
            err = None
            for attempt in range(3):  # Chrome occasionally exits during start-up; try again
                try:
                    ctx = self._pw.chromium.launch_persistent_context(channel="chrome", **kw)
                    self.channel = "chrome"
                    return ctx
                except Exception as e:
                    err = e
                    if "is not found" in str(e) or "Executable doesn't exist" in str(e):
                        break  # not installed: retrying won't help
                    time.sleep(1.5 * (attempt + 1))
                    if self._xvfb and ("Missing X server" in str(e) or self._xvfb.poll() is not None):
                        self._restart_xvfb(kw)
                    # a Chrome that was still closing can leave the profile locked
                    for lock in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
                        try:
                            os.unlink(os.path.join(str(kw.get("user_data_dir", "")), lock))
                        except OSError:
                            pass
            if want == "chrome" or not Path(self._pw.chromium.executable_path).exists():
                raise err  # no fallback browser: report the real Chrome error
            applog.warn(f"Google Chrome unavailable ({str(err).splitlines()[0][:120]}); using bundled Chromium",
                        source="browser", user_id=self._uid())
        elif want and want != "chromium":
            ctx = self._pw.chromium.launch_persistent_context(channel=want, **kw)
            self.channel = want
            return ctx
        self.channel = "chromium"
        return self._pw.chromium.launch_persistent_context(**kw)

    def _restart_xvfb(self, kw):
        try:
            self._xvfb.kill()
            self._xvfb.wait(timeout=5)
        except Exception:
            pass
        self._xvfb, display = start_xvfb()
        kw.setdefault("env", {})["DISPLAY"] = display

    def _note_request(self, req):
        """Remember protection-style request header names (names only) sent to Southwest."""
        try:
            if _site_host() in req.url:
                for name in req.headers:
                    if SHAPE_HEADER.match(name):
                        self._protection_headers.add(SHAPE_HEADER.sub(r"x-…-\1", name))
        except Exception:
            pass

    def protection_signals(self):
        """{vendor: [signal names]} for bot-protection products recognisable from the cookie
        names this browser holds for southwest.com and the request headers its pages sent.
        Names only: values are never read into the log."""
        try:
            names = {c["name"] for c in self._ctx.cookies() if _site_host() in c.get("domain", "")}
        except Exception:
            names = set()
        found = {}
        for vendor, pattern in PROTECTION_COOKIES:
            hits = sorted(n for n in names if pattern.match(n))
            if hits:
                found.setdefault(vendor, []).extend(
                    sorted({re.sub(r"[0-9a-f]{6,}$", "…", h) for h in hits}))
        if getattr(self, "_protection_headers", None):
            found.setdefault("F5 Distributed Cloud / Shape (request headers)", []).extend(
                sorted(self._protection_headers))
        return found

    def _log_protection(self):
        sig = self.protection_signals()
        text = "; ".join(f"{v}: {', '.join(n)}" for v, n in sig.items()) or "none recognised"
        applog.info(f"Bot protection seen on southwest.com this session: {text}", source="browser",
                    user_id=self._uid())

    def __exit__(self, *exc):
        if self._ctx and self._warmed:
            try:
                self._log_protection()
            except Exception:
                pass
        try:
            if self._ctx:
                self.state = self._ctx.storage_state()
        except Exception:
            pass
        try:
            self._ctx and self._ctx.close()
        except Exception:
            pass
        if self._pw:
            self._pw.stop()
        if self._xvfb:
            self._xvfb.terminate()
            try:
                self._xvfb.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._xvfb.kill()
        if self._tmp_profile:
            shutil.rmtree(self._tmp_profile, ignore_errors=True)

    def warm_up(self):
        """Arrive at the home page and look around before the first search, like a visitor would."""
        self._warmed = True
        if not self.humanize:
            return
        try:
            self.page.goto(settings.home_url, wait_until="domcontentloaded", timeout=60_000)
            self.human.read(self.human.rng.uniform(3, 7))
        except Exception as e:
            applog.warn(f"Home page visit failed ({e.__class__.__name__}: {str(e).splitlines()[0][:150]})",
                        source="browser", user_id=self._uid(), attachment=self.capture("home-failed"))

    def ensure_signed_in(self, username, password, allowed, code_provider=None, log=None):
        """Open the home page (this doubles as the warm-up). If the session has lapsed and an
        attempt is allowed, sign in with the saved credentials. Returns a swa_login.LoginResult,
        or one with status 'skipped' when signed out but no attempt was allowed."""
        from . import swa_login

        self._warmed = True
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        rec = swa_login.Recorder(self.page, settings.debug_dir / "signin" / f"{self.profile_key}-{stamp}",
                                 log=log or (lambda m: applog.info(m.split("] ", 1)[-1], source="signin",
                                                                   user_id=self._uid())))
        swa_login.watch_network(self.page, rec)
        try:
            blocked = swa_login.open_home(self.page, self.human, rec, settings.home_url)
            if blocked:
                blocked.screenshot = rec.last
                return blocked
            if swa_login.is_signed_in(self.page):
                rec.log("Session still valid")
                return swa_login.LoginResult("already_signed_in", "session still valid",
                                             swa_login.account_name(self.page))
            if not allowed:
                rec.log("Signed out, but no sign-in attempt allowed now")
                return swa_login.LoginResult("skipped", "signed out; attempt limit reached")
            res = swa_login.login(self.page, self.human, username, password, rec,
                                  code_provider=code_provider, home_url=settings.home_url,
                                  home_opened=True)
            if not res.ok:
                res.screenshot = rec.last
            return res
        except Exception as e:
            return swa_login.LoginResult("error", f"{e.__class__.__name__}: {str(e).splitlines()[0][:200]}",
                                         screenshot=rec.shot("exception"))

    def search(self, origin, dest, date, adults, fare_type):
        tag = f"{origin}-{dest}-{date}-{fare_type}"
        if not self._warmed:
            self.warm_up()
        return self._load(origin, dest, date, adults, fare_type, tag)

    def _parse(self, payload, tag):
        flights = parse_flights(payload)
        if flights is None:
            self._save_json(tag, payload)
            applog.warn(f"{tag}: Southwest's fare data has a shape the parser doesn't know; raw JSON saved "
                        "to debug/", source="search", user_id=self._uid(), attachment=self.capture(tag))
        return flights

    def _load(self, origin, dest, date, adults, fare_type, tag):
        q = search_query(origin, dest, date, adults, fare_type)
        # Loaded like a bookmarked search URL. No Referer: page.goto is an address-bar navigation
        # (Sec-Fetch-Site: none), and a real browser never sends a Referer with one.
        try:
            with self.page.expect_response(
                lambda r: SHOPPING_API in r.url and r.request.method == "POST", timeout=60_000
            ) as info:
                self.page.goto(f"{SEARCH_URL}?{urlencode(q)}", wait_until="domcontentloaded")
            payload = info.value.json()
        except Exception as e:
            if self._no_flights_page(3_000):
                return self._no_flights(tag)
            applog.warn(f"{tag}: no fare data ({e.__class__.__name__}: {str(e).splitlines()[0][:150]}); "
                        f"page {self.page.url[:120]}", source="search", user_id=self._uid(),
                        attachment=self.capture(tag))
            return None
        flights = parse_flights(payload)
        if flights is None and self._no_flights_page(6_000):
            return self._no_flights(tag)
        if self.humanize:
            try:
                self.human.read()  # look over the results before moving on
            except Exception:
                pass
        return self._parse(payload, tag) if flights is None else flights

    def _no_flights_page(self, wait_ms):
        """Southwest sends a day with no flights on that route to the Low Fare Calendar, with a
        "Wanna look for another day?" banner ("We don't have any flights from ... on ...")."""
        try:
            self.page.wait_for_function(
                "() => location.pathname.includes('low-fare-calendar') || "
                "/we don.t have any flights/i.test(document.body ? document.body.innerText : '')",
                timeout=wait_ms)
            return True
        except Exception:
            return False

    def _no_flights(self, tag):
        applog.info(f"{tag}: Southwest has no flights that day", source="search", user_id=self._uid())
        if self.humanize:
            try:
                self.human.read()
            except Exception:
                pass
        return {}

    def _uid(self):
        return self.profile_key if isinstance(self.profile_key, int) else None

    _cal_shown = None        # (origin, dest, month, adults, fare_type) of the calendar on screen
    _cal_toggle_works = None
    _cal_tabs_work = None

    @property
    def calendar_switches_currency(self):
        """True when the next calendar for the same route and month will be a click on the open page."""
        return self._cal_shown is not None and self._cal_toggle_works is not False

    def calendar_can_click(self, origin, dest, date, adults, fare_type):
        """True if that calendar is one click away on the open page: the same route showing,
        and only the month (a month tab) or only the currency (Dollars/Points) differs."""
        sh = self._cal_shown
        if not sh or sh[:2] != (origin, dest) or sh[3] != adults:
            return False
        month_differs, currency_differs = sh[2] != date[:7], sh[4] != fare_type
        if month_differs and not currency_differs:
            return self._cal_tabs_work is not False
        if currency_differs and not month_differs:
            return self._cal_toggle_works is not False
        return False

    def calendar(self, origin, dest, date, adults, fare_type):
        """Lowest fare for each day of the month containing `date`, as {YYYY-MM-DD: amount or None},
        or None if the calendar couldn't be read. When the same route's calendar is already open,
        another month is a click on its month tab, and the other currency a click on its
        Dollars/Points switch, instead of a new page load. Raw data is saved to debug/calendar/."""
        tag = f"calendar-{origin}-{dest}-{date[:7]}-{fare_type}"
        if not self._warmed:
            self.warm_up()
        key = (origin, dest, date[:7], adults)
        if self.calendar_can_click(origin, dest, date, adults, fare_type):
            days = self._calendar_click(date, fare_type, tag)
            if days is not None:
                self._cal_shown = (*key, fare_type)
                return days
        q = {
            "adultPassengersCount": adults, "adultsCount": adults,
            "currencyCode": "PTS" if fare_type == "POINTS" else "USD",
            "departureDate": date, "destinationAirportCode": dest, "fareType": fare_type,
            "lapInfantPassengersCount": 0, "originationAirportCode": origin, "passengerType": "ADULT",
            "promoCode": "", "returnAirportCode": "", "returnDate": "", "tripType": "oneway",
        }
        self._cal_shown = None

        def go():
            self.page.goto(f"{settings.calendar_url}?{urlencode(q)}", wait_until="domcontentloaded")
        try:
            days, payloads = self._collect_calendar(go, tag, wait=CALENDAR_WAIT)
        except Exception as e:
            applog.warn(f"{tag}: calendar didn't load ({e.__class__.__name__}: {str(e).splitlines()[0][:150]}); "
                        f"page {self.page.url[:120]}", source="search", user_id=self._uid(),
                        attachment=self.capture(tag))
            return None
        if days is not None:
            self._cal_shown = (*key, fare_type)
            return days
        # Nothing recognisable: keep every JSON response so the parser can be fixed from them
        raw = self._save_json(tag + "-all", [{"url": u, "data": d} for u, d in payloads], sub="calendar")
        applog.warn(f"{tag}: couldn't find daily fares in {len(payloads)} data response(s)"
                    f"{_api_errors(payloads)}; saved to {raw}. Falling back to searching each date.",
                    source="search", user_id=self._uid(), attachment=self.capture(tag))
        return None

    def _calendar_click(self, date, fare_type, tag):
        """Show another month (its tab) or currency (Dollars/Points) on the open calendar and
        read it. None if the control isn't there or no fares for that month come back; that
        kind of click is then not tried again this session."""
        from .swa_login import find
        month_tab = self._cal_shown[2] != date[:7]
        if month_tab:
            label = dt.date.fromisoformat(date[:7] + "-01").strftime("%B %Y")   # "March 2027"
            el = find(self.page, [("css", f'[role=tab][aria-label="{label}"]')], timeout=3)
            what, pressed = f"the {label} tab", "aria-selected"
        else:
            el = find(self.page, FARE_TOGGLE[fare_type], timeout=3)
            what, pressed = "the Dollars/Points switch", "aria-pressed"
        if el is None or (el.get_attribute(pressed) or "").lower() == "true":
            self._note_click(month_tab, False, f"{tag}: {what} isn't on the calendar page; loading "
                             "its address instead", tag + "-no-control")
            return None

        def click():
            if self.humanize:
                self.human.pause(1.5, 4.5)       # look at this month's prices, then move on
                self.human.click(el)
            else:
                el.click()
        days, payloads = self._collect_calendar(click, tag, wait=20)
        if days and any(d.startswith(date[:7]) for d in days):
            self._note_click(month_tab, True, f"{tag}: clicked {what} on the calendar page")
            return {d: v for d, v in days.items() if d.startswith(date[:7])} if month_tab else days
        self._note_click(month_tab, False, f"{tag}: {what} didn't bring that month's fares"
                         f"{_api_errors(payloads)}; loading its address instead", tag)
        return None

    def _note_click(self, month_tab, worked, message, shot=None):
        attr = "_cal_tabs_work" if month_tab else "_cal_toggle_works"
        first = getattr(self, attr) is None
        setattr(self, attr, worked)
        if worked:
            if first:   # log the first success of each kind per session
                applog.info(message, source="search", user_id=self._uid())
        else:
            applog.warn(message, source="search", user_id=self._uid(),
                        attachment=self.capture(shot) if shot else None)

    def _collect_calendar(self, action, tag, wait):
        """Run `action` (a page load or a click) and read the JSON responses it brings until one
        has daily fares. Returns (days or None, [(url, payload)])."""
        responses = []

        def note(r):
            try:
                if (r.request.resource_type in ("xhr", "fetch") and not is_private(r.url)
                        and "json" in (r.headers.get("content-type") or "") and len(responses) < 60):
                    responses.append(r)
            except Exception:
                pass
        self.page.on("response", note)
        found, checked, payloads = None, 0, []
        try:
            action()
            deadline = time.monotonic() + wait
            while time.monotonic() < deadline:
                while checked < len(responses):
                    r = responses[checked]
                    checked += 1
                    try:
                        payload = r.json()
                    except Exception:
                        continue
                    payloads.append((r.url, payload))
                    found = best_calendar(payloads, final=False)   # Southwest's own format only
                if found:
                    break
                self.page.wait_for_timeout(500)
            if not found:
                found = self._calendar_from_page(tag)                # what the page itself shows
            if not found:
                found = best_calendar(payloads, final=True)        # last resort: the guesser
        finally:
            self.page.remove_listener("response", note)
        if self.humanize:
            try:
                self.human.read()
            except Exception:
                pass
        if not found:
            return None, payloads
        url, payload, days, exact = found
        self._save_json(tag, {"url": url, "data": payload}, sub="calendar")
        if not exact:
            applog.warn(f"{tag}: calendar read by guessing its format from {url.split('?')[0][-60:]}; "
                        "check the saved data", source="search", user_id=self._uid())
        return days, payloads

    def _calendar_from_page(self, tag):
        """Read the fares off the calendar page's day cells when its data response was missed."""
        try:
            got = self.page.evaluate(CALENDAR_DOM_JS)
        except Exception:
            return None
        days = parse_calendar_cells(got.get("month"), got.get("cells"))
        if not days:
            return None
        applog.info(f"{tag}: read the fares from the calendar page itself ({got.get('month')})",
                    source="search", user_id=self._uid())
        return ("page", {"month": got.get("month"), "cells": got.get("cells")}, days, True)

    def capture(self, tag):
        """Screenshot what the browser shows right now, for the log. Returns the file (or None)."""
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
        d = settings.debug_dir / "shots" / dt.date.today().isoformat()
        try:
            d.mkdir(parents=True, exist_ok=True)
            path = d / f"{stamp}-{''.join(c if c.isalnum() or c in '-_' else '-' for c in tag)[:60]}.png"
            self.page.screenshot(path=str(path), full_page=False, timeout=10_000)
            return path
        except Exception:
            return None

    def _save_json(self, tag, payload, sub=""):
        d = settings.debug_dir / sub if sub else settings.debug_dir
        try:
            d.mkdir(parents=True, exist_ok=True)
            path = d / f"{dt.datetime.now():%Y%m%d-%H%M%S}-{tag}.json"
            path.write_text(json.dumps(payload, indent=2))
            return str(path.relative_to(settings.data_dir)) if settings.data_dir in path.parents else str(path)
        except Exception:
            return None


SELFTEST_PAGE = """<!doctype html><title>selftest</title>
<body style="margin:0;height:2400px;font-family:sans-serif">
<input id="q" style="position:absolute;left:300px;top:260px;width:320px;height:36px">
<button id="go" style="position:absolute;left:300px;top:340px;width:140px;height:40px">Go</button>
<script>
window.ev = {moves: 0, trustedClicks: 0, keys: 0, untrusted: 0, wheels: [], dwell: [], shift: 0, cdp: false};
const downAt = {};
addEventListener('mousemove', e => { ev.moves++; if (!e.isTrusted) ev.untrusted++; });
addEventListener('wheel', e => { ev.wheels.push(e.deltaY); });
addEventListener('keydown', e => { ev.keys++; if (!e.isTrusted) ev.untrusted++; if (e.key === 'Shift') ev.shift++; downAt[e.key] = e.timeStamp; });
addEventListener('keyup', e => { if (e.key !== 'Shift' && downAt[e.key] !== undefined) ev.dwell.push(e.timeStamp - downAt[e.key]); });
document.getElementById('go').addEventListener('click', e => { if (e.isTrusted) ev.trustedClicks++; });
// DevTools-protocol leak check: the console serializer reads .stack only when automation listens
setInterval(() => { const e = new Error(); Object.defineProperty(e, 'stack', {get() { ev.cdp = true; return ''; }}); console.debug(e); }, 100);
</script>"""


def selftest(verbose=True):
    """Launch the browser exactly as searches do, then exercise human input on a local page.
    Fails if a page could see automation: navigator.webdriver, Headless UA, untrusted or
    zero-length key presses, capitals without Shift, uneven wheel steps, trimmed languages,
    or the DevTools-protocol console leak."""
    import random
    import statistics

    from .human import Human

    with BrowserSearcher(human=True) as b:
        p = b.page
        p.set_content(SELFTEST_PAGE)
        h = Human(p, rng=random.Random(7), typo_rate=0.25)  # normal speed: key hold times are checked
        text = "Dallas Love Field, 2026"
        h.type(p.locator("#q"), text)
        h.click(p.locator("#go"))
        h.scroll(300)
        time.sleep(0.4)
        info = p.evaluate("""() => {
            const gl = document.createElement('canvas').getContext('webgl');
            const dbg = gl && gl.getExtension('WEBGL_debug_renderer_info');
            return {value: document.getElementById('q').value, ev: window.ev,
            webdriver: navigator.webdriver, ua: navigator.userAgent, languages: navigator.languages,
            brands: navigator.userAgentData ? navigator.userAgentData.brands.map(b => b.brand) : [],
            inner: [innerWidth, innerHeight], outer: [outerWidth, outerHeight],
            screen: [screen.width, screen.height], tz: Intl.DateTimeFormat().resolvedOptions().timeZone,
            gl: dbg ? gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL) : null,
            automationGlobals: Object.keys(window).filter(k => /playwright|__pw|cdc_|puppeteer/i.test(k))};
        }""")
        problems = []
        ev = info["ev"]
        if info["value"] != text:
            problems.append(f"typed {info['value']!r}, expected {text!r}")
        if info["webdriver"]:
            problems.append("navigator.webdriver is true")
        if "Headless" in info["ua"]:
            problems.append("user agent says Headless")
        if ev["trustedClicks"] != 1 or ev["untrusted"] or ev["moves"] < 10 or ev["keys"] < len(text):
            problems.append(f"unexpected input events (clicks {ev['trustedClicks']}, untrusted {ev['untrusted']}, "
                            f"moves {ev['moves']}, keys {ev['keys']})")
        dwell = statistics.median(ev["dwell"]) if ev["dwell"] else 0
        if dwell < 30:
            problems.append(f"keys held only {dwell:.0f} ms (people: ~50-150 ms)")
        if ev["shift"] < 3:  # D, L, F
            problems.append("capital letters typed without Shift")
        if len({abs(w) for w in ev["wheels"]}) > 1:
            problems.append(f"uneven wheel steps {sorted(set(ev['wheels']))}")
        if list(info["languages"])[:2] != ["en-US", "en"]:
            problems.append(f"navigator.languages {info['languages']} (normal Chrome: ['en-US', 'en'])")
        if ev["cdp"]:
            problems.append("DevTools-protocol console leak detected")
        chrome_ui = info["outer"][1] - info["inner"][1]
        if chrome_ui > 120:  # tabs + toolbar are ~80-95 px; more means a warning bar is showing
            problems.append(f"browser UI is {chrome_ui} px tall: a warning/info bar is showing")
        if info["automationGlobals"]:
            problems.append(f"automation globals {info['automationGlobals']}")
        if verbose:
            print(f"browser: {b.channel}  ua: {info['ua']}")
            print(f"brands: {info['brands']}  languages: {info['languages']}  tz: {info['tz']}")
            print(f"window: inner {info['inner']} outer {info['outer']} screen {info['screen']} "
                  f"(browser UI {info['outer'][1] - info['inner'][1]} px)")
            print(f"webgl: {info['gl']}")
            print(f"input: key hold median {dwell:.0f} ms, shift presses {ev['shift']}, wheel steps "
                  f"{sorted(set(ev['wheels']))}, mouse moves {ev['moves']}, clicks {ev['trustedClicks']}, "
                  f"cdp leak {ev['cdp']}")
            if info["gl"] and "SwiftShader" in info["gl"]:
                print("note: WebGL is software-rendered (SwiftShader); sites can tell there's no GPU")
        if problems:
            raise SystemExit("selftest FAILED: " + "; ".join(problems))
    print("selftest ok")
