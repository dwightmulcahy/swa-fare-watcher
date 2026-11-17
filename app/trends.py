"""Price trend per flight: each upcoming flight's price history on this watch, how far it is above
its low, its 7-day move, and (with enough history) how similar flights on this watch moved from
the same distance to departure."""
import datetime as dt
from statistics import median

from . import db

BUCKETS = ((0, 6, "this week"), (7, 13, "1-2 weeks out"), (14, 27, "2-4 weeks out"),
           (28, 59, "1-2 months out"), (60, 10_000, "2+ months out"))
MIN_SAMPLES = 10      # departed flights needed before an outlook is shown
SHOW = 40


def _bucket(days):
    return next(i for i, (lo, hi, _) in enumerate(BUCKETS) if lo <= days <= hi)


def _spark(series, w=90, h=22):
    """SVG polyline points for a small price line (low = bottom)."""
    vals = [c for _, c in series]
    if len(vals) < 2:
        return ""
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1
    step = w / (len(vals) - 1)
    return " ".join(f"{i * step:.1f},{h - 2 - (v - lo) / span * (h - 4):.1f}" for i, v in enumerate(vals))


def flight_trends(watch_id, today=None):
    today = dt.date.fromisoformat(today or db.today())
    daily = {}   # (leg, date, flight_key) -> {day: lowest cash that day}
    meta = {}
    for r in db.query("SELECT leg, date, flight_key, flights, depart, origin, dest, ts, cash FROM fares "
                      "WHERE watch_id=? AND cash IS NOT NULL ORDER BY ts", (watch_id,)):
        k = (r["leg"], r["date"], r["flight_key"])
        d = daily.setdefault(k, {})
        day = r["ts"][:10]
        d[day] = min(d.get(day, r["cash"]), r["cash"])
        meta[k] = r
    moves = {}   # bucket -> [final price - price then] for flights that have left
    for (leg, date, _), d in daily.items():
        dep = dt.date.fromisoformat(date)
        if dep >= today or len(d) < 2:
            continue
        final = d[max(d)]
        for day, cash in d.items():
            out = (dep - dt.date.fromisoformat(day)).days
            if out >= 1 and day != max(d):
                moves.setdefault(_bucket(out), []).append(final - cash)
    last_seen = max((max(d) for d in daily.values()), default=None)
    rows = []
    for k, d in daily.items():
        leg, date, _ = k
        dep = dt.date.fromisoformat(date)
        if dep < today or max(d) != last_seen:      # left, or not offered in the latest search
            continue
        series = sorted(d.items())
        cur = series[-1][1]
        low, high = min(d.values()), max(d.values())
        week_ago = (dt.date.fromisoformat(series[-1][0]) - dt.timedelta(days=7)).isoformat()
        before = [c for day, c in series if day <= week_ago]
        b = _bucket((dep - today).days)
        m = moves.get(b, [])
        outlook = None
        if len(m) >= MIN_SAMPLES:
            med = median(m)
            outlook = (f"similar flights {BUCKETS[b][2]} usually " +
                       (f"rose ${med:,.0f}" if med >= 1 else f"fell ${-med:,.0f}" if med <= -1 else "held steady")
                       + f" by departure ({len(m)} seen)")
        f = meta[k]
        rows.append({"leg": leg, "date": date, "flights": f["flights"], "depart": f["depart"],
                     "route": f"{f['origin']}→{f['dest']}" if f["origin"] and f["dest"] else "",
                     "cash": cur, "low": low, "high": high, "above_low": cur - low,
                     "week": (cur - before[-1]) if before else None, "seen": len(series),
                     "spark": _spark(series[-21:]), "outlook": outlook,
                     "rising": bool(m) and len(m) >= MIN_SAMPLES and median(m) >= 1})
    rows.sort(key=lambda r: (r["date"], r["leg"], r["depart"] or "", r["cash"]))
    return rows[:SHOW]
