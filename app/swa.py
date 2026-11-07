"""Southwest search, parsing, filtering and cash-vs-points scoring."""
import datetime as dt
import json
import os
import select
import shutil
import subprocess
import sys
from urllib.parse import urlencode

from .config import settings

SEARCH_URL = "https://www.southwest.com/air/booking/select-depart.html"
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
def watch_dates(w, today=None):
    """Dates a watch searches. `w` is a mapping with watch columns."""
    today = today or dt.date.today()
    if w["dates"]:
        ds = sorted({dt.date.fromisoformat(d.strip()) for d in w["dates"].split(",") if d.strip()})
    else:
        a, b = w["days_from"] or 0, w["days_to"] or 0
        ds = [today + dt.timedelta(days=i) for i in range(a, b + 1)]
    if w["weekdays"]:
        keep = {WEEKDAYS.index(x) for x in w["weekdays"].split(",") if x in WEEKDAYS}
        ds = [d for d in ds if d.weekday() in keep]
    return [d.isoformat() for d in ds if d >= today][: settings.max_dates_per_watch]


def searches_per_run(w, today=None) -> int:
    return 2 * len(watch_dates(w, today))


def passes(r, w):
    if w["max_stops"] is not None and r["stops"] > w["max_stops"]:
        return False
    if w["max_duration_min"] and r["duration"] > w["max_duration_min"]:
        return False
    if w["depart_after"] and r["depart"] < w["depart_after"]:
        return False
    if w["depart_before"] and r["depart"] > w["depart_before"]:
        return False
    max_price = w["max_price"] if w["max_price"] is not None else float("inf")
    max_points = w["max_points"] if w["max_points"] is not None else float("inf")
    cash_ok = r["cash"] is not None and r["cash"] <= max_price
    pts_ok = (
        r["points"] is not None
        and r["points"] <= max_points
        and (r["cpp"] is None or r["cpp"] >= (w["min_cpp"] or 0))
    )
    return cash_ok or pts_ok


def recommend(r, w):
    if r["cpp"] is None:
        return "Cash" if r["cash"] is not None else "Points"
    return "Points" if r["cpp"] >= (w["min_cpp"] or 0) else "Cash"


def booking_url(origin, dest, date, adults=1):
    return f"{SEARCH_URL}?" + urlencode({
        "originationAirportCode": origin, "destinationAirportCode": dest,
        "departureDate": date, "tripType": "oneway", "adultPassengersCount": adults,
    })


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
def start_xvfb(timeout=10):
    """Start a private virtual display so Chromium can run headed without a desktop.
    Returns (process, ':N')."""
    if not shutil.which("Xvfb"):
        raise RuntimeError("Xvfb not installed: install it or set SWA_HEADLESS=true")
    r, w = os.pipe()
    proc = subprocess.Popen(
        ["Xvfb", "-displayfd", str(w), "-screen", "0", "1366x900x24", "-nolisten", "tcp"],
        pass_fds=(w,), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    os.close(w)
    try:
        ready, _, _ = select.select([r], [], [], timeout)
        num = os.read(r, 32).decode().strip() if ready else ""
    finally:
        os.close(r)
    if not num.isdigit():
        proc.kill()
        raise RuntimeError("Xvfb did not start")
    return proc, f":{num}"


class BrowserSearcher:
    """One Chromium context per user run; loads and returns that user's Southwest session."""

    def __init__(self, state=None):
        self.state = state
        self._pw = self._browser = self._ctx = self.page = None

    _xvfb = None

    def __enter__(self):
        from playwright.sync_api import sync_playwright

        kw = {"headless": settings.headless}
        if settings.browser_channel:
            kw["channel"] = settings.browser_channel
        if not settings.headless and not os.environ.get("DISPLAY") and sys.platform.startswith("linux"):
            self._xvfb, display = start_xvfb()
            kw["env"] = {**os.environ, "DISPLAY": display}
        try:
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(**kw)
            self._ctx = self._browser.new_context(
                storage_state=self.state, viewport={"width": 1366, "height": 900}, locale="en-US")
            self.page = self._ctx.new_page()
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *exc):
        try:
            if self._ctx:
                self.state = self._ctx.storage_state()
        except Exception:
            pass
        for closer in (self._ctx, self._browser):
            try:
                closer and closer.close()
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

    def search(self, origin, dest, date, adults, fare_type):
        q = {
            "adultPassengersCount": adults, "departureDate": date,
            "departureTimeOfDay": "ALL_DAY", "destinationAirportCode": dest,
            "fareType": fare_type, "originationAirportCode": origin,
            "passengerType": "ADULT", "returnDate": "", "tripType": "oneway", "reset": "true",
        }
        tag = f"{origin}-{dest}-{date}-{fare_type}"
        try:
            with self.page.expect_response(
                lambda r: SHOPPING_API in r.url and r.request.method == "POST", timeout=60_000
            ) as info:
                self.page.goto(f"{SEARCH_URL}?{urlencode(q)}", wait_until="domcontentloaded")
            payload = info.value.json()
        except Exception as e:
            print(f"[warn] {tag}: no fare data ({e.__class__.__name__})", file=sys.stderr)
            self._dump(tag)
            return None
        flights = parse_flights(payload)
        if flights is None:
            print(f"[warn] {tag}: unrecognized response shape", file=sys.stderr)
            self._dump(tag, payload)
        return flights

    def _dump(self, tag, payload=None):
        d = settings.debug_dir
        d.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        try:
            if payload is not None:
                (d / f"{stamp}-{tag}.json").write_text(json.dumps(payload, indent=2))
            else:
                self.page.screenshot(path=str(d / f"{stamp}-{tag}.png"), full_page=True)
        except Exception:
            pass


def selftest():
    """Launch Chromium exactly as searches do (headed in Xvfb unless SWA_HEADLESS) and exit."""
    with BrowserSearcher() as b:
        b.page.set_content("<title>ok</title>")
        assert b.page.title() == "ok"
    print("selftest ok")
