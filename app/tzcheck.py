"""Make sure the browser's time zone matches where this machine's internet connection is.

The browser reports its time zone to every site (Date().getTimezoneOffset(), Intl), and bot
detection compares it with the location of the IP address the request came from. A server in
Texas whose browser says UTC (Docker's default) or Asia/Tokyo looks like a bot or a proxy.

This looks up the public IP's time zone, compares UTC offsets now and six months from now (so
both standard and daylight time must agree), and refuses to start on a mismatch.
"""
import datetime as dt
import json
import os
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import settings

# Tried in order; each returns the caller's public IP and its IANA time zone. No keys needed.
LOOKUPS = [
    ("ipinfo.io", "https://ipinfo.io/json",
     lambda d: (d.get("timezone"), d.get("ip"), ", ".join(x for x in (d.get("city"), d.get("region"), d.get("country")) if x))),
    ("ipapi.co", "https://ipapi.co/json/",
     lambda d: (d.get("timezone"), d.get("ip"), ", ".join(x for x in (d.get("city"), d.get("region"), d.get("country_name")) if x))),
    ("worldtimeapi.org", "https://worldtimeapi.org/api/ip",
     lambda d: (d.get("timezone"), d.get("client_ip"), "")),
]


class Result:
    def __init__(self, level, message, browser_tz=None, ip_tz=None, ip=None, place=None, source=None):
        self.level, self.message = level, message  # level: ok, warn, error
        self.browser_tz, self.ip_tz, self.ip, self.place, self.source = browser_tz, ip_tz, ip, place, source

    @property
    def ok(self):
        return self.level != "error"

    def __repr__(self):
        return f"Result({self.level!r}, {self.message!r})"


def browser_timezone():
    """The IANA zone the browser will use: SWA_TIMEZONE, else TZ, else the system zone."""
    for name in (settings.browser_timezone, os.environ.get("TZ")):
        if name:
            return name.lstrip(":")
    for path in ("/etc/timezone",):
        try:
            v = Path(path).read_text().strip()
            if v:
                return v
        except OSError:
            pass
    try:  # /etc/localtime -> .../zoneinfo/America/Chicago (Linux and macOS)
        target = os.path.realpath("/etc/localtime")
        if "zoneinfo/" in target:
            return target.split("zoneinfo/", 1)[1]
    except OSError:
        pass
    return None


def lookup_ip_timezone(timeout=6, opener=None):
    """(tz, ip, place, source) for this machine's public IP, or None if no service answered."""
    opener = opener or urllib.request.urlopen
    for source, url, parse in LOOKUPS:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "swa-fare-watcher tz-check",
                                                       "Accept": "application/json"})
            with opener(req, timeout=timeout) as r:
                tz, ip, place = parse(json.loads(r.read().decode()))
            if tz:
                return tz, ip, place, source
        except Exception:
            continue
    return None


def _offsets(name, now):
    z = ZoneInfo(name)
    return tuple(int((now + dt.timedelta(days=d)).astimezone(z).utcoffset().total_seconds() // 60) for d in (0, 182))


def _fmt(minutes):
    sign = "+" if minutes >= 0 else "-"
    return f"UTC{sign}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}"


def check(lookup=lookup_ip_timezone, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    btz = browser_timezone()
    if not btz:
        return Result("error", "No time zone is set. Set TZ (e.g. TZ=America/Chicago) to where this "
                               "machine's internet connection is.")
    try:
        b_off = _offsets(btz, now)
    except (ZoneInfoNotFoundError, ValueError):
        return Result("error", f"Unknown time zone {btz!r}. Use an IANA name like America/Chicago.", btz)
    found = lookup()
    if not found:
        return Result("warn", f"Couldn't look up this machine's internet location, so the browser time zone "
                              f"({btz}) wasn't verified.", btz)
    itz, ip, place, source = found
    where = f"{ip or 'this IP'}{f' ({place})' if place else ''}"
    try:
        i_off = _offsets(itz, now)
    except (ZoneInfoNotFoundError, ValueError):
        return Result("warn", f"{source} reported an unknown time zone {itz!r} for {where}; not verified.",
                      btz, itz, ip, place, source)
    if b_off == i_off:
        same = "" if itz == btz else f" (same offsets as {itz})"
        return Result("ok", f"Browser time zone {btz}{same} matches {where}.", btz, itz, ip, place, source)
    var = "SWA_TIMEZONE" if settings.browser_timezone else "TZ"
    return Result(
        "error",
        f"Browser time zone {btz} ({_fmt(b_off[0])}) doesn't match this machine's internet location: "
        f"{where} is in {itz} ({_fmt(i_off[0])}), per {source}. Sites compare the two, and a mismatch "
        f"looks like a bot or proxy. Set {var}={itz}, or SWA_SKIP_TZ_CHECK=true if you know better."
        + (_not_passed_hint(var) if btz.upper() in ("UTC", "ETC/UTC") else ""),
        btz, itz, ip, place, source)


def in_docker():
    return Path("/.dockerenv").exists()


def _not_passed_hint(var):
    # The image defaults to UTC; seeing it usually means the .env never reached the container
    if not in_docker():
        return ""
    return (f" In Docker, UTC means {var} isn't reaching the container. Check the service has `env_file:` "
            "pointing at your .env by a path that exists from the compose file's folder (with "
            "`required: false` a missing file is skipped silently; Container Station keeps pasted compose "
            "files in its own folder, so use the full path), or set `environment:` with "
            f"`{var}: ...`. The NAS's own time zone setting doesn't matter.")


last_result = None  # shown on the admin page


def enforce(log=print):
    """Run the check; returns False (caller should exit) on a mismatch unless skipped."""
    global last_result
    if os.environ.get("SWA_SKIP_TZ_CHECK", "").strip().lower() in ("1", "true", "yes", "on"):
        last_result = Result("warn", "Time zone check skipped (SWA_SKIP_TZ_CHECK).", browser_timezone())
        log("[tz-check] skipped (SWA_SKIP_TZ_CHECK)")
        return True
    last_result = check()
    log(f"[tz-check] {last_result.level.upper()}: {last_result.message}")
    try:
        from . import db
        db.init()
        with db.tx() as con:
            con.execute("INSERT INTO logs(ts, level, source, message) VALUES (?,?,?,?)",
                        (db.now(), {"ok": "info"}.get(last_result.level, last_result.level), "tz-check",
                         last_result.message))
    except Exception:
        pass
    return last_result.ok
