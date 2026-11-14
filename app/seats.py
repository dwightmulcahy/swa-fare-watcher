"""Seat preferences and choosing a seat from Southwest's seat map.

Southwest labels every seat button, e.g.
  "Seat 5E  $0. (Chargeable, Front cabin, Extra leg room, Middle)"
  "Seat 11F Selected for passenger number 2 NAME. $0. (Chargeable, Front cabin, Preferred,
   Window without window view, Over wing, Window) unavailable"
so a seat's type, position, price and availability come straight from its aria-label.
"""
import datetime as dt
import re
from zoneinfo import ZoneInfo

# Rapid Rewards status / card: which seat types its holder can get free when selection opens
STATUSES = {
    "none": ("No status or card", {"extra", "preferred", "standard"}),
    "plus_premier": ("Rapid Rewards Plus or Premier card", {"preferred", "standard"}),
    "priority": ("Priority or Performance Business card", {"extra", "preferred", "standard"}),
    "a_list": ("A-List", {"extra", "preferred", "standard"}),
    "a_list_preferred": ("A-List Preferred", {"extra", "preferred", "standard"}),
}

POSITIONS = {"window": "Window", "aisle": "Aisle", "any": "No preference"}

# Extra wishes on top of window/aisle (form checkbox name -> label)
WISHES = {
    "extra_legroom": "Extra Legroom",
    "exit_row": "Exit row",
    "bulkhead": "Bulkhead (front row of a cabin)",
    "front": "As far forward as possible",
    "no_middle": "Never a middle seat",
    "window_view": "Avoid windows without a view",
}

DEFAULT_PREFS = {"position": "window", "extra_legroom": True, "front": True, "no_middle": True,
                 "window_view": True, "exit_row": False, "bulkhead": False}

OPEN_HOURS = 48          # seat selection (free upgrades) opens this long before departure

_LABEL = re.compile(r"^Seat\s+(\d+)([A-K])\b(?P<rest>.*)$", re.S)


def parse_seat(label):
    """A seat button's aria-label -> {"seat", "row", "col", "price", "available", "selected",
    "type", "features"} or None if it isn't a seat."""
    m = _LABEL.match((label or "").strip())
    if not m:
        return None
    rest = m.group("rest")
    feats = re.search(r"\(([^)]*)\)", rest)
    features = {f.strip().lower() for f in feats.group(1).split(",")} if feats else set()
    price = re.search(r"\$\s*([\d,]+(?:\.\d+)?)", rest)
    kind = ("extra" if "extra leg room" in features else
            "preferred" if "preferred" in features else "standard")
    return {
        "seat": f"{m.group(1)}{m.group(2)}", "row": int(m.group(1)), "col": m.group(2),
        "price": float(price.group(1).replace(",", "").rstrip(".") or 0) if price else None,
        "available": not rest.rstrip().endswith("unavailable"),
        "selected": "selected for passenger" in rest.lower(),
        "type": kind, "features": features,
    }


def prefs_of(w):
    """A watch's seat preferences, with defaults filled in."""
    import json
    try:
        p = json.loads((w["seat_prefs"] if "seat_prefs" in w.keys() else None) or "{}")
    except (ValueError, TypeError, AttributeError):
        p = {}
    return {**DEFAULT_PREFS, **(p if isinstance(p, dict) else {})}


def score(seat, prefs):
    """How much a seat suits these preferences (higher is better)."""
    f = seat["features"]
    s = 0.0
    pos = prefs.get("position", "any")
    is_window, is_aisle, is_middle = "window" in f, "aisle" in f, "middle" in f
    if pos == "window" and is_window:
        s += 30
    elif pos == "aisle" and is_aisle:
        s += 30
    if is_middle and prefs.get("no_middle"):
        s -= 60
    if prefs.get("window_view") and "window without window view" in f:
        s -= 25
    if prefs.get("extra_legroom"):
        s += {"extra": 40, "preferred": 12, "standard": 0}[seat["type"]]
    else:
        s += {"extra": 5, "preferred": 5, "standard": 0}[seat["type"]]
    if "exit row" in f:
        s += 20 if prefs.get("exit_row") else 0
    if "bulkhead seat" in f:
        s += 10 if prefs.get("bulkhead") else 0
    s -= seat["row"] * (1.0 if prefs.get("front") else 0.05)   # forward wins ties either way
    return s


def choose(seats, prefs, status="none", taken=()):
    """The best free (price 0) available seat this status can get, or None."""
    allowed = STATUSES.get(status, STATUSES["none"])[1]
    ok = [s for s in seats if s["available"] and s["price"] == 0 and s["type"] in allowed
          and s["seat"] not in taken]
    return max(ok, key=lambda s: score(s, prefs), default=None)


def better(new, current, prefs, margin=1.0):
    """Is moving from `current` (None = no seat yet) to `new` an improvement?"""
    if new is None:
        return False
    return current is None or score(new, prefs) > score(current, prefs) + margin


def describe(seat):
    f = seat["features"]
    pos = "Window" if "window" in f else "Aisle" if "aisle" in f else "Middle"
    kind = {"extra": "Extra Legroom", "preferred": "Preferred", "standard": "Standard"}[seat["type"]]
    extra = [x for x, k in (("exit row", "exit row"), ("bulkhead", "bulkhead seat")) if k in f]
    return f"{seat['seat']} ({', '.join([kind, pos, *extra])})"


def airport_tz(code):
    """IANA time zone of an airport, or None."""
    try:
        import airportsdata
    except ImportError:
        return None
    global _AIRPORTS
    if _AIRPORTS is None:
        _AIRPORTS = airportsdata.load("IATA")
    a = _AIRPORTS.get((code or "").upper())
    return a["tz"] if a else None


_AIRPORTS = None


def opens_at(date, time, origin):
    """When seat selection opens (UTC) for a flight leaving `origin` at local `date` `time`."""
    tz = airport_tz(origin)
    if not tz or not date or not time:
        return None
    local = dt.datetime.combine(dt.date.fromisoformat(date), dt.time.fromisoformat(time),
                                tzinfo=ZoneInfo(tz))
    return (local - dt.timedelta(hours=OPEN_HOURS)).astimezone(dt.timezone.utc)
