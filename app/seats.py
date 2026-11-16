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

# What can matter about a seat (key -> label). The user ranks the ones they care about; a higher
# ranked wish always beats everything ranked below it.
CRITERIA = {
    "no_middle": "Not a middle seat",
    "seat_type": "My seat position (window / aisle above)",
    "extra_legroom": "Extra Legroom (Preferred counts half)",
    "exit_row": "Exit row",
    "bulkhead": "Bulkhead (front row of a cabin)",
    "window_view": "A window that has a view",
    "front": "As far forward as possible",
}
WISHES = CRITERIA                       # older name

DEFAULT_ORDER = ["no_middle", "extra_legroom", "seat_type", "window_view", "front"]
DEFAULT_PREFS = {"position": "window", "order": DEFAULT_ORDER}
_BASE = 4                               # each rank weighs 4x the next: beats all lower ranks together

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
    """A watch's seat preferences: {"position", "order": [criteria, best first], <criterion>: bool}.
    Older watches stored a flag per wish; those become an order in the old priority."""
    import json
    try:
        p = json.loads((w["seat_prefs"] if "seat_prefs" in w.keys() else None) or "{}")
    except (ValueError, TypeError, AttributeError):
        p = {}
    p = p if isinstance(p, dict) else {}
    pos = p.get("position") if p.get("position") in POSITIONS else DEFAULT_PREFS["position"]
    if isinstance(p.get("order"), list):
        order = [k for k in dict.fromkeys(p["order"]) if k in CRITERIA]
    elif p:
        old = ["no_middle", "extra_legroom", "seat_type", "exit_row", "window_view", "bulkhead", "front"]
        order = [k for k in old if k == "seat_type" or p.get(k)]
    else:
        order = list(DEFAULT_ORDER)
    return normalize(pos, order)


def normalize(position, order):
    order = [k for k in order if k in CRITERIA and not (k == "seat_type" and position == "any")]
    return {"position": position, "order": order, **{k: k in order for k in CRITERIA}}


def _value(seat, key, prefs):
    """0..1: how well a seat meets one criterion."""
    f = seat["features"]
    if key == "seat_type":
        pos = prefs.get("position", "any")
        return 1.0 if pos in ("window", "aisle") and pos in f else 0.0
    if key == "no_middle":
        return 0.0 if "middle" in f else 1.0
    if key == "extra_legroom":
        return {"extra": 1.0, "preferred": 0.5, "standard": 0.0}[seat["type"]]
    if key == "exit_row":
        return 1.0 if "exit row" in f else 0.0
    if key == "bulkhead":
        return 1.0 if "bulkhead seat" in f else 0.0
    if key == "window_view":
        return 0.0 if "window without window view" in f else 1.0
    if key == "front":
        return max(0.0, 1.0 - (seat["row"] - 1) / 40)
    return 0.0


def _weights(prefs):
    order = prefs.get("order") or []
    return [(k, _BASE ** (len(order) - 1 - i)) for i, k in enumerate(order)]


def fit(seat, prefs):
    """How well a seat matches the ranked preferences, 0-100."""
    w = _weights(prefs)
    total = sum(x for _, x in w)
    return 100.0 * sum(_value(seat, k, prefs) * x for k, x in w) / total if total else 0.0


def score(seat, prefs):
    """Ranking key (higher is better): the fit, then forward rows break ties."""
    return fit(seat, prefs) - seat["row"] * 1e-4


def choose(seats, prefs, status="none", taken=()):
    """The best free (price 0) available seat this status can get, or None."""
    allowed = STATUSES.get(status, STATUSES["none"])[1]
    ok = [s for s in seats if s["available"] and s["price"] == 0 and s["type"] in allowed
          and s["seat"] not in taken]
    return max(ok, key=lambda s: score(s, prefs), default=None)


def ranked(seats, prefs, status="none", taken=()):
    """Free (price 0) available seats this status can get, best first."""
    allowed = STATUSES.get(status, STATUSES["none"])[1]
    ok = [s for s in seats if s["available"] and s["price"] == 0 and s["type"] in allowed
          and s["seat"] not in taken]
    return sorted(ok, key=lambda s: -score(s, prefs))


def better(new, current, prefs):
    """Is moving from `current` (None = no seat yet) to `new` an improvement on what's ranked
    (not just a row or two further forward when "front" isn't asked for)?"""
    if new is None:
        return False
    return current is None or fit(new, prefs) > fit(current, prefs) + 1e-6


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
