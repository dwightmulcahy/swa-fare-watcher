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

# Rapid Rewards status / card: (label, seat types free at booking, seat types free within 48 h
# of departure), per southwest.com/seats. The map's $0 price is what finally decides; these keep
# it from taking a seat the status shouldn't get.
_ALL = {"extra", "preferred", "standard"}
STATUSES = {
    "none": ("No status or card (seats your fare includes)", set(), _ALL),
    "plus": ("Rapid Rewards Plus or Plus Business card", set(), {"standard"}),
    "premier": ("Rapid Rewards Premier or Premier Business card", set(), {"preferred", "standard"}),
    "priority": ("Priority or Performance Business card", {"preferred", "standard"}, _ALL),
    "a_list": ("A-List", {"preferred", "standard"}, _ALL),
    "a_list_preferred": ("A-List Preferred", _ALL, _ALL),
}
OLD_STATUSES = {"plus_premier": "premier"}


def status_of(status):
    status = OLD_STATUSES.get(status, status)
    return status if status in STATUSES else "none"


def allowed(status, window=True):
    """Seat types this status gets free now: at booking, or (window) within 48 h of departure."""
    _, booking, late = STATUSES[status_of(status)]
    return booking | late if window else booking


def free_early(status):
    """Does this status get free seats before the 48-hour window (worth checking right away)?"""
    return bool(STATUSES[status_of(status)][1])


def window_gain(status):
    """Seat types the 48-hour window adds for this status (worth being there as it opens)."""
    _, booking, late = STATUSES[status_of(status)]
    return late - booking

POSITIONS = {"window": "Window", "aisle": "Aisle", "any": "No preference"}

# Seat selection priorities: complete combinations of seat type and position, each given a
# priority 1-8 (equal allowed) or Off. A seat takes the best priority of the categories it matches,
# and a lower-priority seat never beats a higher-priority one, whatever else it has going for it.
# A bulkhead seat (front row of the cabin: nothing to stow under the seat ahead) counts as its own
# seat type, so it can be ranked or switched off apart from other Extra Legroom seats.
CATEGORIES = {
    "exit_window_open": "Exit-row window, no seat in front",
    "exit_window": "Exit-row window",
    "extra_window": "Extra-legroom window",
    "bulkhead_window": "Bulkhead window",
    "exit_aisle": "Exit-row aisle",
    "extra_aisle": "Extra-legroom aisle",
    "bulkhead_aisle": "Bulkhead aisle",
    "std_window": "Standard window",
    "std_aisle": "Standard aisle",
    "middle": "Middle seat",
}
DEFAULT_PRIORITIES = {k: i + 1 for i, k in enumerate(CATEGORIES)}
MAX_PRIORITY = len(CATEGORIES)
# Only between seats of the same priority, in the order the user sets (seat number is always last)
TIEBREAKS = {
    "position": "Your window / aisle choice",
    "legroom": "More legroom (Extra Legroom, no seat in front)",
    "neighbor": "An empty seat next to it",
    "forward": "Further forward in the cabin",
    "view": "A window with a view",
    "recline": "A seat that reclines",
}
LIMITED_RECLINE = "limited recline"
EMPTY_NEIGHBOR = "empty seat next to it"
NEXT_TO = {"A": "B", "C": "B", "D": "E", "F": "E", "B": "AC", "E": "DF"}   # same side of the aisle
DEFAULT_TIEBREAK = list(TIEBREAKS)
DEFAULT_PREFS = {"version": 2, "position": "window", "priorities": dict(DEFAULT_PRIORITIES),
                 "tiebreak": list(DEFAULT_TIEBREAK), "fallback": False, "explain": True}

def _preset(order, off=("middle",), position="window"):
    pri = {k: None for k in CATEGORIES}
    pri.update({k: i + 1 for i, k in enumerate(order)})
    for k in off:
        pri[k] = None
    return {**DEFAULT_PREFS, "priorities": pri, "position": position}


# Ready-made Seat Selection Priorities (users can also save their own)
BUILTIN_PRESETS = {
    "Window first": _preset(["exit_window_open", "exit_window", "extra_window", "bulkhead_window", "std_window",
                             "exit_aisle", "extra_aisle", "bulkhead_aisle", "std_aisle"]),
    "Aisle first": _preset(["exit_aisle", "extra_aisle", "bulkhead_aisle", "std_aisle", "exit_window_open",
                            "exit_window", "extra_window", "bulkhead_window", "std_window"], position="aisle"),
    "Most legroom": _preset(["exit_window_open", "exit_window", "exit_aisle", "extra_window", "extra_aisle",
                             "bulkhead_window", "bulkhead_aisle", "std_window", "std_aisle"]),
    "Extra legroom only": _preset(["exit_window_open", "exit_window", "exit_aisle", "extra_window", "extra_aisle",
                                   "bulkhead_window", "bulkhead_aisle"], off=("std_window", "std_aisle", "middle")),
}

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


OPEN_FRONT = "no seat in front"


def annotate(seat_map):
    """Mark exit-row window seats with no seat in front of them: on bigger planes the row ahead of an exit
    leaves out its window seats for the door, so the one behind gets the open floor. The row ahead
    must have seats (not a galley or the front of the cabin)."""
    have = {(s["row"], s["col"]) for s in seat_map}
    rows = {s["row"] for s in seat_map}
    for s in seat_map:
        ahead = s["row"] - 1
        if ("window" in s["features"] and "exit row" in s["features"] and ahead in rows and (ahead, s["col"]) not in have
                and sum(1 for r, _ in have if r == ahead) >= 2):
            s["features"].add(OPEN_FRONT)
    # the seat(s) beside it on its side of the aisle are free (your own seat counts as free: you'd leave it)
    free = {(s["row"], s["col"]) for s in seat_map if s.get("available") or s.get("selected")}
    for s in seat_map:
        beside = [(s["row"], c) for c in NEXT_TO.get(s["col"], "") if (s["row"], c) in have]
        if beside and all(b in free for b in beside):
            s["features"].add(EMPTY_NEIGHBOR)
    return seat_map


def prefs_of(w):
    """A watch's seat selection priorities (see DEFAULT_PREFS). Watches saved before priorities
    existed are converted from their ranked wishes (see _migrate); "migrated" marks that."""
    import json
    try:
        p = json.loads((w["seat_prefs"] if "seat_prefs" in w.keys() else None) or "{}")
    except (ValueError, TypeError, AttributeError):
        p = {}
    p = p if isinstance(p, dict) else {}
    if p and p.get("version") != 2:
        p = _migrate(p)
    return normalize(p)


def normalize(p):
    """Fill in and clean a priorities dict."""
    pos = p.get("position") if p.get("position") in POSITIONS else DEFAULT_PREFS["position"]
    pri = {}
    given = dict(p.get("priorities")) if isinstance(p.get("priorities"), dict) else dict(DEFAULT_PRIORITIES)
    # saved before bulkhead had its own categories: those seats counted as Extra-legroom, so keep that
    for bulk, extra in (("bulkhead_window", "extra_window"), ("bulkhead_aisle", "extra_aisle")):
        if bulk not in given:
            given[bulk] = given.get(extra)
    for k in CATEGORIES:
        v = given.get(k)
        pri[k] = v if isinstance(v, int) and 1 <= v <= MAX_PRIORITY else None
    tb = [k for k in dict.fromkeys(p.get("tiebreak") if isinstance(p.get("tiebreak"), list) else DEFAULT_TIEBREAK)
          if k in TIEBREAKS]
    if isinstance(p.get("tiebreak"), list) and "recline" not in p["tiebreak"] and not p.get("tiebreak_v2"):
        tb.append("recline")           # added later: on for lists saved before it existed
    if isinstance(p.get("tiebreak"), list) and "neighbor" not in p["tiebreak"] and not p.get("tiebreak_v3"):
        tb.append("neighbor")          # added later: on (last) for lists saved before it existed
    out = {"version": 2, "tiebreak_v2": True, "tiebreak_v3": True, "position": pos, "priorities": pri, "tiebreak": tb,
           "fallback": bool(p.get("fallback", False)), "explain": bool(p.get("explain", True))}
    if p.get("migrated"):
        out["migrated"] = True
    return out


def to_json(p):
    import json
    return json.dumps({k: v for k, v in normalize(p).items() if k != "migrated"})


# ---------- classification ----------
def position_of(seat):
    """window / aisle / middle, or None when the map doesn't say (never guessed)."""
    f = seat["features"]
    return next((x for x in ("window", "aisle", "middle") if x in f), None)


def attributes(seat):
    """What's known about a seat; anything the map doesn't state is left out."""
    f = seat["features"]
    pos = position_of(seat)
    out = {"position": pos, "exit": "exit row" in f, "extra": seat["type"] == "extra",
           "open_front": OPEN_FRONT in f, "bulkhead": "bulkhead seat" in f}
    out["view"] = None if pos != "window" else "window without window view" not in f
    out["recline"] = False if LIMITED_RECLINE in f else None      # only known when it doesn't
    out["neighbor_free"] = EMPTY_NEIGHBOR in f
    return out


def matches(seat, cat):
    a = attributes(seat)
    pos, bulk = a["position"], a["bulkhead"]
    extra = a["extra"] and not bulk
    std = not a["extra"] and not a["exit"] and not bulk
    return {
        "exit_window_open": a["exit"] and pos == "window" and a["open_front"],
        "exit_window": a["exit"] and pos == "window",
        "extra_window": extra and pos == "window",
        "bulkhead_window": bulk and pos == "window",
        "exit_aisle": a["exit"] and pos == "aisle",
        "extra_aisle": extra and pos == "aisle",
        "bulkhead_aisle": bulk and pos == "aisle",
        "std_window": std and pos == "window",
        "std_aisle": std and pos == "aisle",
        "middle": pos == "middle",
    }[cat]


def priority(seat, prefs):
    """(priority, category) of the best enabled category the seat matches, or (None, None)."""
    best = (None, None)
    for cat, p in prefs["priorities"].items():
        if p is not None and matches(seat, cat) and (best[0] is None or p < best[0]):
            best = (p, cat)
    return best


def _tiebreak(seat, prefs):
    a, out = attributes(seat), []
    for k in prefs.get("tiebreak") or []:
        if k == "position":
            want = prefs.get("position")
            out.append(0 if want not in ("window", "aisle") or a["position"] == want else 1)
        elif k == "legroom":
            out.append(0 if a["extra"] or a["open_front"] else 1)
        elif k == "forward":
            out.append((seat["row"] - 1) // 10)       # front / middle / back of the cabin, not row by row
        elif k == "view":
            out.append(1 if a["view"] is False else 0)
        elif k == "recline":
            out.append(1 if a["recline"] is False else 0)
        elif k == "neighbor":
            out.append(0 if a["neighbor_free"] else 1)
    return tuple(out)


def key(seat, prefs):
    """Sort key, lower is better: priority (unmatched last), then the tie-breakers, then seat."""
    p, _ = priority(seat, prefs)
    return (p if p is not None else MAX_PRIORITY + 1, *_tiebreak(seat, prefs), seat["row"], seat["col"])


def fit(seat, prefs):
    """0-100 for colours and lists: priority 1 = 100 ... priority 8 = 0; unmatched = 0."""
    p, _ = priority(seat, prefs)
    return 0.0 if p is None else round(100.0 * (MAX_PRIORITY - p) / (MAX_PRIORITY - 1), 1)


score = fit


def ranked(seats, prefs, status="none", taken=(), window=True):
    """Free (price 0) available seats this status can get now, best first. Seats matching none of
    the enabled priorities only with prefs["fallback"]."""
    kinds = allowed(status, window)
    ok = [s for s in seats if s["available"] and s["price"] == 0 and s["type"] in kinds
          and s["seat"] not in taken and (prefs.get("fallback") or priority(s, prefs)[0] is not None)]
    return sorted(ok, key=lambda s: key(s, prefs))


def choose(seats, prefs, status="none", taken=(), window=True):
    """The best free seat this status can get, or None."""
    ok = ranked(seats, prefs, status, taken, window)
    return ok[0] if ok else None


def better(new, current, prefs):
    """Is moving from `current` (None = no seat yet) to `new` an improvement? A better priority,
    or the same priority and a better tie-breaker (never just another seat number)."""
    if new is None:
        return False
    if current is None:
        return True
    pn, pc = priority(new, prefs)[0], priority(current, prefs)[0]
    if pn is None:
        return False if pc is not None else bool(prefs.get("fallback")) and _tiebreak(new, prefs) < _tiebreak(current, prefs)
    if pc is None or pn < pc:
        return True
    return pn == pc and _tiebreak(new, prefs) < _tiebreak(current, prefs)


def label(seat, prefs):
    """'Priority 3: Extra-legroom window', or why it matches none."""
    p, cat = priority(seat, prefs)
    if p is None:
        return "Not in your priorities"
    return f"Priority {p}: {CATEGORIES[cat]}"


def explain(chosen, prefs, fallback=None):
    """Why this seat: 'Selected: 21A. Matched Priority 3: Extra-legroom window. Higher-ranked
    Exit-row window seats were unavailable.'"""
    p, cat = priority(chosen, prefs)
    if p is None:
        off = [CATEGORIES[c] for c, v in prefs["priorities"].items() if v is None and matches(chosen, c)]
        why = f" ({', '.join(off)} is Off)" if off else ""
        return (f"Selected: {chosen['seat']} as a fallback: it matches none of your priorities{why}. "
                "Taken because Allow fallback seats is on.")
    higher = [CATEGORIES[c] for c, v in sorted(prefs["priorities"].items(), key=lambda kv: kv[1] or 99)
              if v is not None and v < p]
    text = f"Selected: {chosen['seat']}. Matched Priority {p}: {CATEGORIES[cat]}."
    if higher:
        text += f" Higher-ranked {', '.join(higher)} seats were unavailable."
    if "neighbor" in (prefs.get("tiebreak") or []) and EMPTY_NEIGHBOR in chosen["features"]:
        text += " The seat next to it is free (for now)."
    return text


def best_priority(prefs):
    """The best (lowest) enabled priority number, or None."""
    vals = [v for v in prefs["priorities"].values() if v is not None]
    return min(vals) if vals else None


def best_on_plane(seat_map, prefs, status="none"):
    """The best priority this plane has at all (taken or free) among seat types the status gets free:
    with no "exit row, no seat in front" on board, Priority 2 is as good as it gets. None if unknown."""
    types = allowed(status, True)
    vals = [priority(s, prefs)[0] for s in seat_map or [] if s.get("type") in types]
    vals = [v for v in vals if v is not None]
    return min(vals) if vals else None


# ---------- converting the earlier ranked wishes ----------
def _legacy_fit(seat, old):
    """The scoring before priorities existed: each ranked wish outweighs everything below it."""
    order = [k for k in (old.get("order") or []) if k != "seat_type" or old.get("position") in ("window", "aisle")]
    f = seat["features"]

    def value(k):
        return {"seat_type": 1.0 if old.get("position") in f else 0.0,
                "no_middle": 0.0 if "middle" in f else 1.0,
                "extra_legroom": {"extra": 1.0, "preferred": 0.5, "standard": 0.0}[seat["type"]],
                "exit_row": 1.0 if "exit row" in f else 0.0,
                "open_front": 1.0 if OPEN_FRONT in f else 0.0,
                "bulkhead": 1.0 if "bulkhead seat" in f else 0.0,
                "window_view": 0.0 if "window without window view" in f else 1.0,
                "front": 0.0}.get(k, 0.0)
    return sum(value(k) * 4 ** (len(order) - 1 - i) for i, k in enumerate(order))


def _migrate(old):
    """Priorities from the earlier ranked wishes: each category ranked by how a typical seat of it
    scored under them (equal scores share a priority). "Never a middle seat" turns Middle seat Off,
    as it said; nothing else is switched off. Kept marked so the form can say so."""
    if "order" not in old:      # the first version: a flag per wish
        legacy = ["no_middle", "extra_legroom", "seat_type", "exit_row", "window_view", "bulkhead", "front"]
        old = {"position": old.get("position", "window"),
               "order": [k for k in legacy if k == "seat_type" or old.get(k)]}
    reps = {
        "exit_window_open": ("extra", {"window", "exit row", OPEN_FRONT}),
        "exit_window": ("extra", {"window", "exit row"}), "extra_window": ("extra", {"window"}),
        "exit_aisle": ("extra", {"aisle", "exit row"}), "extra_aisle": ("extra", {"aisle"}),
        "bulkhead_window": ("extra", {"window", "bulkhead seat"}), "bulkhead_aisle": ("extra", {"aisle", "bulkhead seat"}),
        "std_window": ("standard", {"window"}), "std_aisle": ("standard", {"aisle"}),
        "middle": ("standard", {"middle"}),
    }
    scores = {c: _legacy_fit({"type": t, "features": f, "row": 10}, old) for c, (t, f) in reps.items()}
    levels = sorted(set(scores.values()), reverse=True)
    pri = {c: levels.index(v) + 1 for c, v in scores.items()}
    if "no_middle" in (old.get("order") or []):
        pri["middle"] = None
    tb = (["position"] if old.get("position") in ("window", "aisle") else []) + ["legroom", "neighbor", "forward", "view",
                                                                                "recline"]
    return {"version": 2, "position": old.get("position", "window"), "priorities": pri, "tiebreak": tb,
            "fallback": False, "explain": True, "migrated": True}


def describe(seat):
    f = seat["features"]
    pos = "Window" if "window" in f else "Aisle" if "aisle" in f else "Middle"
    kind = {"extra": "Extra Legroom", "preferred": "Preferred", "standard": "Standard"}[seat["type"]]
    extra = [x for x, k in (("exit row", "exit row"), ("bulkhead", "bulkhead seat"), (OPEN_FRONT, OPEN_FRONT))
             if k in f]
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
DOMESTIC = {"US", "PR", "VI", "GU", "AS", "MP"}


def international(origin, dest):
    """Does a flight leave or reach a country other than the US (and its territories)? Unknown
    airports count as international, the safer guess for Southwest's earlier change cut-off."""
    try:
        import airportsdata
    except ImportError:
        return True
    global _AIRPORTS
    if _AIRPORTS is None:
        _AIRPORTS = airportsdata.load("IATA")
    countries = [(_AIRPORTS.get((c or "").upper()) or {}).get("country") for c in (origin, dest)]
    return not all(c in DOMESTIC for c in countries)


# ---------- Southwest's planes ----------
# Cabin layouts under assigned seating (Upgraded Points, Feb 2026; seatmaps.com; the seat maps
# users saved): seat types by row, exit rows, seats left out, windows without a view, and rows
# that recline little or not at all. Seat maps don't say which seats recline, so it's added from
# here when the map names the plane. Layouts can change: the map's own labels always win.
PLANES = {
    "Boeing 737 MAX 8": {
        "rows": 30, "extra": set(range(1, 6)) | {14, 15, 16}, "extra_seats": {"6A", "6B", "6C"},
        "preferred": set(range(7, 14)) | {6}, "exits": {14, 15, 16}, "skip": {"15A", "15F"},
        "no_view": {"10A", "11A", "11F"}, "no_recline": {13, 14, 15, 30},
    },
    "Boeing 737-800": {
        "rows": 30, "extra": set(range(1, 6)) | {14, 15, 16}, "extra_seats": {"6A", "6B", "6C"},
        "preferred": set(range(7, 14)) | {6}, "exits": {14, 15}, "skip": set(),
        "no_view": {"10A", "11A", "11F"}, "no_recline": {13, 14, 15, 30},
    },
    # rows 1-9 then 14-27 (Southwest skips 10-13 on the -700), a full row 1, one exit row (15),
    # as on the seat map of a -700 a user saved
    "Boeing 737-700": {
        "rows": 27, "row_list": list(range(1, 10)) + list(range(14, 28)), "full_first_row": True,
        "extra": set(range(1, 6)) | {15}, "extra_seats": {"6D"},
        "preferred": {6, 7, 8, 9, 14}, "exits": {15}, "skip": set(),
        "no_view": set(), "no_recline": {14, 27},
    },
}
PLANES["Boeing 737 MAX 7"] = PLANES["Boeing 737-700"]          # replacing the -700s, same cabin for now

_PLANE_NAME = re.compile(r"Boeing\s*737[\s-]*(MAX\s*([78])|([78]00))", re.I)


def plane_name(text):
    """'Boeing 737 MAX 8' etc. from page text such as 'Boeing 737 MAX8 (subject to change)'."""
    m = _PLANE_NAME.search(text or "")
    if not m:
        return None
    return f"Boeing 737 MAX {m.group(2)}" if m.group(2) else f"Boeing 737-{m.group(3)}"


def add_plane_facts(seat_map, plane):
    """Mark the rows that recline little or not at all (known per plane; the last row always)."""
    if not seat_map:
        return seat_map
    rows = PLANES.get(plane, {}).get("no_recline", set()) | {max(s["row"] for s in seat_map)}
    for s in seat_map:
        if s["row"] in rows:
            s["features"].add(LIMITED_RECLINE)
    return seat_map


def _example(plane):
    L, out = PLANES[plane], []
    full = L.get("full_first_row")
    for r in L.get("row_list") or range(1, L["rows"] + 1):
        for c in "ABCDEF":
            code = f"{r}{c}"
            if (r == 1 and c in "ABC" and not full) or code in L["skip"]:
                continue
            f = {"window" if c in "AF" else "aisle" if c in "CD" else "middle"}
            if r in L["exits"]:
                f.add("exit row")
            if r == 1 or (r == 2 and c in "ABC" and not full):   # the first row on each side: 1D-F and
                f.add("bulkhead seat")                             # 2A-C when row 1 is DEF only
            if code in L["no_view"]:
                f.add("window without window view")
            kind = ("extra" if r in L["extra"] or code in L["extra_seats"] else
                    "preferred" if r in L["preferred"] else "standard")
            out.append({"seat": code, "row": r, "col": c, "type": kind, "features": f,
                        "price": 0, "available": True, "selected": False})
    return add_plane_facts(annotate(out), plane)


def example_maps():
    """{plane: [seat, ...]} with features as sorted lists (JSON for the form's heat map)."""
    names = ["Boeing 737 MAX 8", "Boeing 737-800", "Boeing 737-700"]
    return {k: [{**s, "features": sorted(s["features"])} for s in _example(k)] for k in names}


def heat_maps(w=None):
    """Maps for the form's heat map: this watch's real seat maps first (as last seen), then the
    plane layouts. {name: [seat, ...]}"""
    import json
    out = {}
    try:
        real = json.loads((w["seat_maps"] if w is not None and "seat_maps" in w.keys() else None) or "[]")
    except (ValueError, TypeError, AttributeError):
        real = []
    for m in real:
        when = (m.get("read") or "")[:10]
        name = f"Your flight: {m['flight']} · {m.get('plane') or 'plane not named'}" + (f" (seen {when})" if when else "")
        out[name] = m["seats"]
    return {**out, **example_maps()}


def occupancy(w):
    """Free and taken seats on each of the watch's flights, from the seat maps last seen:
    [{"flight", "plane", "free", "taken", "total", "full" (percent), "free_types" {type: n}, "read"}]."""
    import json
    try:
        maps = json.loads((w["seat_maps"] if w is not None and "seat_maps" in w.keys() else None) or "[]")
    except (ValueError, TypeError, AttributeError):
        return []
    out = []
    for m in maps:
        ss = [s for s in m.get("seats") or [] if s.get("seat")]
        if not ss:
            continue
        free = [s for s in ss if s.get("available") and not s.get("selected")]
        types = {}
        for s in free:
            types[s.get("type") or "Standard"] = types.get(s.get("type") or "Standard", 0) + 1
        out.append({"flight": m.get("flight") or "Flight", "plane": m.get("plane"), "free": len(free),
                    "taken": len(ss) - len(free), "total": len(ss),
                    "full": round(100 * (len(ss) - len(free)) / len(ss)), "free_types": types,
                    "read": (m.get("read") or "")[:16].replace("T", " ")})
    return out


def status_table():
    """{status: {"booking": [...], "window": [...]}} free seat types, for the heat map."""
    return {k: {"booking": sorted(b), "window": sorted(b | w)} for k, (_, b, w) in STATUSES.items()}


def opens_at(date, time, origin, hours=OPEN_HOURS):
    """When seat selection opens (UTC) for a flight leaving `origin` at local `date` `time`
    (or, with `hours`, that long before departure)."""
    tz = airport_tz(origin)
    if not tz or not date or not time:
        return None
    local = dt.datetime.combine(dt.date.fromisoformat(date), dt.time.fromisoformat(time),
                                tzinfo=ZoneInfo(tz))
    return (local - dt.timedelta(hours=hours)).astimezone(dt.timezone.utc)
