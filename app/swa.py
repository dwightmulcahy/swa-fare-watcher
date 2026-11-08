"""Southwest search, parsing, filtering and cash-vs-points scoring."""
import datetime as dt
import hashlib
import json
import os
import select
import shutil
import subprocess
import sys
import tempfile
import time
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


def start_xvfb(timeout=10):
    """Start a private virtual display so Chrome can run headed without a desktop.
    Returns (process, ':N')."""
    if not shutil.which("Xvfb"):
        raise RuntimeError("Xvfb not installed: install it or set SWA_HEADLESS=true")
    r, w = os.pipe()
    proc = subprocess.Popen(
        ["Xvfb", "-displayfd", str(w), "-screen", "0", f"{SCREEN[0]}x{SCREEN[1]}x24", "-nolisten", "tcp"],
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
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def _launch(self, kw):
        want = settings.browser_channel
        if want in ("auto", "chrome"):
            try:
                ctx = self._pw.chromium.launch_persistent_context(channel="chrome", **kw)
                self.channel = "chrome"
                return ctx
            except Exception as e:
                if want == "chrome":
                    raise
                print(f"[browser] Google Chrome unavailable ({str(e).splitlines()[0][:120]}); "
                      "using bundled Chromium", file=sys.stderr)
        elif want and want != "chromium":
            ctx = self._pw.chromium.launch_persistent_context(channel=want, **kw)
            self.channel = want
            return ctx
        self.channel = "chromium"
        return self._pw.chromium.launch_persistent_context(**kw)

    def __exit__(self, *exc):
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
            print(f"[warn] warm-up visit failed ({e.__class__.__name__})", file=sys.stderr)

    def ensure_signed_in(self, username, password, allowed, code_provider=None, log=None):
        """Open the home page (this doubles as the warm-up). If the session has lapsed and an
        attempt is allowed, sign in with the saved credentials. Returns a swa_login.LoginResult,
        or one with status 'skipped' when signed out but no attempt was allowed."""
        from . import swa_login

        self._warmed = True
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        rec = swa_login.Recorder(self.page, settings.debug_dir / "signin" / f"{self.profile_key}-{stamp}",
                                 log=log or (lambda m: print(f"[signin {self.profile_key}] {m}", file=sys.stderr)))
        swa_login.watch_network(self.page, rec)
        try:
            blocked = swa_login.open_home(self.page, self.human, rec, settings.home_url)
            if blocked:
                return blocked
            if swa_login.is_signed_in(self.page):
                rec.log("Session still valid")
                return swa_login.LoginResult("already_signed_in", "session still valid",
                                             swa_login.account_name(self.page))
            if not allowed:
                rec.log("Signed out, but no sign-in attempt allowed now")
                return swa_login.LoginResult("skipped", "signed out; attempt limit reached")
            return swa_login.login(self.page, self.human, username, password, rec,
                                   code_provider=code_provider, home_url=settings.home_url,
                                   home_opened=True)
        except Exception as e:
            rec.shot("exception")
            return swa_login.LoginResult("error", f"{e.__class__.__name__}: {str(e).splitlines()[0][:200]}")

    def search(self, origin, dest, date, adults, fare_type):
        q = {
            "adultPassengersCount": adults, "departureDate": date,
            "departureTimeOfDay": "ALL_DAY", "destinationAirportCode": dest,
            "fareType": fare_type, "originationAirportCode": origin,
            "passengerType": "ADULT", "returnDate": "", "tripType": "oneway", "reset": "true",
        }
        tag = f"{origin}-{dest}-{date}-{fare_type}"
        if not self._warmed:
            self.warm_up()
        # Loaded like a bookmarked search URL. No Referer: page.goto is an address-bar navigation
        # (Sec-Fetch-Site: none), and a real browser never sends a Referer with one.
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
        if self.humanize:
            try:
                self.human.read()  # look over the results before moving on
            except Exception:
                pass
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
            fonts: ['Arial', 'Helvetica', 'Times New Roman', 'Courier New', 'Verdana', 'Georgia',
                    'DejaVu Sans', 'Liberation Sans', 'Noto Sans'].filter(f => document.fonts.check(`12px "${f}"`)),
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
        if info["automationGlobals"]:
            problems.append(f"automation globals {info['automationGlobals']}")
        if verbose:
            print(f"browser: {b.channel}  ua: {info['ua']}")
            print(f"brands: {info['brands']}  languages: {info['languages']}  tz: {info['tz']}")
            print(f"window: inner {info['inner']} outer {info['outer']} screen {info['screen']}")
            print(f"webgl: {info['gl']}")
            print(f"fonts: {len(info['fonts'])}/9 {info['fonts']}")
            print(f"input: key hold median {dwell:.0f} ms, shift presses {ev['shift']}, wheel steps "
                  f"{sorted(set(ev['wheels']))}, mouse moves {ev['moves']}, clicks {ev['trustedClicks']}, "
                  f"cdp leak {ev['cdp']}")
            if info["gl"] and "SwiftShader" in info["gl"]:
                print("note: WebGL is software-rendered (SwiftShader); sites can tell there's no GPU")
        if problems:
            raise SystemExit("selftest FAILED: " + "; ".join(problems))
    print("selftest ok")
