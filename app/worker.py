"""Background worker: runs one user's watches at a time in a single browser, plus a daily scheduler."""
import datetime as dt
import hashlib
import html
import json
import math
import queue
import random
import threading
import time

from . import applog, db, mailer, seats, swa
from .config import settings
from .security import decrypt_json, encrypt_json


def seconds_until(hhmm, now=None):
    now = now or dt.datetime.now()
    h, m = map(int, hhmm.split(":"))
    nxt = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if nxt <= now:
        nxt += dt.timedelta(days=1)
    return (nxt - now).total_seconds()


def day_window(hhmm, jitter_min, now=None, last_day=None):
    """The next daily window (day, start, end) still ahead of `now`: hh:mm ± jitter_min, skipping
    days already run. If the window has already started, it begins a minute from now."""
    now = now or dt.datetime.now()
    h, m = map(int, hhmm.split(":"))
    for add in range(0, 3):
        day = now.date() + dt.timedelta(days=add)
        if last_day is not None and day <= last_day:
            continue
        base = dt.datetime.combine(day, dt.time(h, m))
        lo, hi = base - dt.timedelta(minutes=jitter_min), base + dt.timedelta(minutes=jitter_min)
        if hi <= now:
            continue  # today's window is over
        return day, max(lo, now + dt.timedelta(seconds=60)), hi
    raise RuntimeError("no next window")  # unreachable with sane input


def plan_day(user_ids, lo, hi, rng=random):
    """Give each user their own random start time inside the window, so several accounts
    never search back to back from the same IP. Returns [(time, user_id)] sorted by time."""
    span = max((hi - lo).total_seconds(), 0)
    plan = [((lo + dt.timedelta(seconds=rng.uniform(0, span))).replace(microsecond=0), uid) for uid in user_ids]
    return sorted(plan, key=lambda x: x[0])


def user_offset(user_id, spread_min):
    """Each user's own usual time of day: a fixed offset (minutes) from SWA_DAILY_AT, seeded from
    the user id, so accounts on one IP keep different habits (one checks at 6:40, another 8:55)."""
    if not spread_min:
        return 0.0
    h = int(hashlib.sha256(f"schedule:{user_id}".encode()).hexdigest(), 16)
    return (h % 10_000) / 10_000 * 2 * spread_min - spread_min


def plan_users(user_ids, day, now, hhmm, spread_min, jitter_min, min_gap_min=20, rng=random):
    """Today's start time per user: their usual time ± a fresh daily jitter, never in the past,
    and at least min_gap_min apart so two accounts never search back to back from one IP."""
    h, m = map(int, hhmm.split(":"))
    base = dt.datetime.combine(day, dt.time(h, m))
    plan = []
    for uid in user_ids:
        t = base + dt.timedelta(minutes=user_offset(uid, spread_min) + rng.uniform(-jitter_min, jitter_min))
        plan.append((max(t, now + dt.timedelta(seconds=60)).replace(microsecond=0), uid))
    plan.sort(key=lambda x: x[0])
    for i in range(1, len(plan)):
        gap = plan[i - 1][0] + dt.timedelta(minutes=min_gap_min)
        if plan[i][0] < gap:
            plan[i] = (gap + dt.timedelta(seconds=rng.randint(0, 300)), plan[i][1])
    return plan


# habitual times of day: (name, earliest, latest) in minutes after midnight
SLOTS_WEEKDAY = [("morning", 6 * 60 + 30, 8 * 60 + 45), ("lunch", 12 * 60, 13 * 60 + 30),
                 ("evening", 19 * 60, 21 * 60 + 45)]
SLOTS_WEEKEND = [("morning", 8 * 60 + 45, 10 * 60 + 45), ("afternoon", 14 * 60, 16 * 60 + 30),
                 ("evening", 19 * 60 + 30, 22 * 60)]


def habit_slots(user_id, day, jitter_min=20, rng=random):
    """This user's check-in times on `day`: 2-3 habitual slots chosen once per user (seeded from
    the id), each at the user's usual minute within it, moved up to ±jitter_min each day."""
    r = random.Random(int(hashlib.sha256(f"slots:{user_id}".encode()).hexdigest(), 16))
    weekend = day.weekday() >= 5
    table = SLOTS_WEEKEND if weekend else SLOTS_WEEKDAY
    n = 3 if r.random() < 0.4 else 2
    names = sorted(r.sample(range(3), n))
    if 2 not in names and r.random() < 0.7:   # most people look in the evening
        names = sorted(set(names[:-1]) | {2})
    out = []
    for i in names:
        _, lo, hi = table[i]
        usual = r.uniform(lo, hi)
        m = min(max(usual + rng.uniform(-jitter_min, jitter_min), lo - jitter_min), hi + jitter_min)
        out.append(dt.datetime.combine(day, dt.time(0)) + dt.timedelta(minutes=m, seconds=rng.randint(0, 59)))
    return out


def plan_slots(user_ids, day, now, min_gap_min=20, rng=random):
    """Every (time, user) check-in for `day` still ahead of `now`, at least min_gap_min apart."""
    plan = sorted((t, uid) for uid in user_ids for t in habit_slots(uid, day, rng=rng) if t > now)
    for i in range(1, len(plan)):
        gap = plan[i - 1][0] + dt.timedelta(minutes=min_gap_min)
        if plan[i][0] < gap:
            plan[i] = (gap + dt.timedelta(seconds=rng.randint(0, 300)), plan[i][1])
    return plan


def skip_today(user_id, day, chance, rng=random):
    """Now and then a user doesn't check at all that day (never two days running), like a person.
    Users with a booked flight to watch for rebooking are never skipped."""
    if chance <= 0 or db.get_state(f"skipped:{user_id}") == (day - dt.timedelta(days=1)).isoformat():
        return False
    if db.one("SELECT 1 FROM watches WHERE user_id=? AND active=1 AND booked IS NOT NULL AND booked<>'' "
              "LIMIT 1", (user_id,)):
        return False
    if rng.random() < chance:
        db.set_state(f"skipped:{user_id}", day.isoformat())
        return True
    return False


def next_run_time(hhmm, jitter_min, now=None, last_day=None, rng=random):
    """A single run time for the next window (one user's plan)."""
    _, lo, hi = day_window(hhmm, jitter_min, now, last_day)
    return plan_day([0], lo, hi, rng)[0][0] if hi > lo else hi.replace(microsecond=0)


def search_pause(lo, hi, rng=random):
    """Seconds to wait between searches: log-normal around the middle of (lo, hi), so most gaps
    are similar, some are short, a few are long; plus an occasional real break."""
    median = (lo + hi) / 2
    d = min(max(rng.lognormvariate(math.log(median), 0.45), lo * 0.6), hi * 3)
    if rng.random() < 0.07:
        d += rng.uniform(45, 180)  # stepped away / got distracted
    return d


def search_order(dates, rng=random):
    """The order to look at a watch's dates: usually earliest first, sometimes latest first,
    sometimes jumping around, like a person comparing days."""
    r = rng.random()
    if r < 0.6:
        return list(dates)
    if r < 0.8:
        return list(reversed(dates))
    out = list(dates)
    rng.shuffle(out)
    return out


# ----- which dates to search today (scheduled runs)
HOT_HOURS = 36        # after a new low fare, look at that date again for this long...
HOT_AGAIN_HOURS = 4   # ...as soon as this many hours after the last look (the next check-in)


def search_every(days_out, quiet_days=0):
    """How many days between full searches of a date this far away: fares months out barely move,
    and a date whose cheapest price hasn't moved for a week or two is looked at less (interest fades).
    Dates within 10 days are always daily."""
    every = _base_every(days_out)
    if days_out > 10:
        every *= 3 if quiet_days >= 14 else 2 if quiet_days >= 7 else 1
    return min(every, 10)


def _base_every(days_out):
    if days_out <= 14:
        return 1
    if days_out <= 45:
        return 2
    if days_out <= 120:
        return 3
    return 5


def due_legs(w, legs, today=None, now=None):
    """The (leg, date)s of a watch due a search today (none twice in a day). A watch with only a
    handful of dates searches each daily; a rebook watch searches everything every time."""
    today = today or dt.date.today()
    if (w["kind"] or "fare") != "fare":
        return list(legs)
    small = len(legs) <= 8     # a handful of dates: each one daily
    now = now or dt.datetime.now()
    last = {(r["leg"], r["date"]): r for r in
            db.query("SELECT * FROM searched WHERE watch_id=?", (w["id"],))}
    out = []
    for leg, d in legs:
        days_out = (dt.date.fromisoformat(d) - today).days
        row = last.get((leg, d))
        if row is None:
            out.append((leg, d))
            continue
        seen = dt.datetime.fromisoformat(row["ts"])
        if row["hot_until"] and dt.datetime.fromisoformat(row["hot_until"]) > now \
                and now - seen >= dt.timedelta(hours=HOT_AGAIN_HOURS):
            out.append((leg, d))          # a fresh deal: back for another look at the next check-in
            continue
        quiet = (now - dt.datetime.fromisoformat(row["changed_at"])).days if row["changed_at"] else 0
        every = 1 if small else search_every(days_out, quiet)
        if (today - seen.date()).days >= every:
            out.append((leg, d))
    return out


def mark_hot(con, hits, now=None):
    """The dates of new deals stay 'hot' for HOT_HOURS: looked at again at the next check-in."""
    until = ((now or dt.datetime.now()) + dt.timedelta(hours=HOT_HOURS)).isoformat(timespec="seconds")
    for h in hits:
        if h.get("rebook"):
            continue
        t = h.get("trip")
        for leg, d in ((("out", t["out_date"]), ("ret", t["ret_date"])) if t else (("out", h.get("date")),)):
            if d:
                con.execute("UPDATE searched SET hot_until=? WHERE watch_id=? AND leg=? AND date=?",
                            (until, h["watch_id"], leg, d))


def page_budget(user_id, day=None):
    """Today's page-load budget for this user: random in SWA_DAILY_PAGE_BUDGET, fixed for the day."""
    lo, hi = settings.page_budget
    if hi <= 0:
        return None
    day = day or db.today()
    return random.Random(f"budget:{user_id}:{day}").randint(lo, hi)


def fit_budget(plan, room):
    """Keep the most useful dates that fit in `room` page loads (2 per airport pair per date):
    nearest departures first. The kept dates stay in their mixed-up order."""
    if room is None:
        return plan
    hot = {(r["watch_id"], r["leg"], r["date"]) for r in
           db.query("SELECT watch_id, leg, date FROM searched WHERE hot_until > ?", (db.now(),))}
    ranked = sorted((((w["id"], leg, d) not in hot), d, w["id"], leg, len(swa.routes(w)))
                    for w, legs in plan for leg, d in legs)
    keep, used = set(), 0
    for _, d, wid, leg, n in ranked:
        if used + 2 * n > room:
            continue
        keep.add((wid, leg, d))
        used += 2 * n
    return [(w, [x for x in legs if (w["id"], *x) in keep]) for w, legs in plan]


def record_searched(con, watch_id, leg, date, rows):
    """Remember a date was searched today, and whether its cheapest prices moved."""
    cash = min((r["cash"] for r in rows if r.get("cash") is not None), default=None)
    points = min((r["points"] for r in rows if r.get("points") is not None), default=None)
    now = db.now()
    old = con.execute("SELECT cash, points, changed_at FROM searched WHERE watch_id=? AND leg=? AND date=?",
                      (watch_id, leg, date)).fetchone()
    changed = old is None or (old["cash"], old["points"]) != (cash, points)
    con.execute("INSERT INTO searched(watch_id, leg, date, ts, cash, points, changed_at) VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(watch_id, leg, date) DO UPDATE SET ts=excluded.ts, cash=excluded.cash, "
                "points=excluded.points, changed_at=excluded.changed_at",
                (watch_id, leg, date, now, cash, points, now if changed else old["changed_at"]))


def calendar_unchanged(con, watch_id, leg, date, low, before, recent_days=3):
    """True if this date's calendar low is the same as at the previous calendar read and the date
    was fully searched in the last few days: nothing new to find there."""
    prev = con.execute("SELECT MIN(cash) c, MIN(points) p FROM calendar_fares WHERE watch_id=? AND leg=? "
                       "AND date=? AND ts=(SELECT MAX(ts) FROM calendar_fares WHERE watch_id=? AND leg=? "
                       "AND date=? AND ts<?)", (watch_id, leg, date, watch_id, leg, date, before)).fetchone()
    if not prev or (prev["c"], prev["p"]) == (None, None) or (prev["c"], prev["p"]) != (low.get("cash"), low.get("points")):
        return False
    seen = con.execute("SELECT ts FROM searched WHERE watch_id=? AND leg=? AND date=?",
                       (watch_id, leg, date)).fetchone()
    return bool(seen) and (dt.datetime.now() - dt.datetime.fromisoformat(seen["ts"])).days < recent_days


class VisitOver(Exception):
    """A one-visit run used its visit; the run winds down and keeps what it found."""


class Visits:
    """The browser for one run, split into visits like a person's: after a handful of searches
    (SWA_VISIT_SEARCHES) the browser closes, the worker takes a break (SWA_VISIT_BREAK minutes),
    then a new visit starts at the home page with the same profile. Everything else is passed
    through to the current searcher."""

    def __init__(self, worker, user_id, factory, state, remaining=lambda: 0, rng=random, single=False):
        self._worker, self._uid, self._factory, self._rng = worker, user_id, factory, rng
        self._single = single
        self._remaining = remaining
        self._inner = None
        self._state = state
        self._shape_errors = 0
        self.visits = 0
        self._new_quota()

    def _new_quota(self):
        lo, hi = settings.visit_searches
        self._quota = self._rng.randint(lo, hi) if hi > 0 else None
        self._count = 0

    def __enter__(self):
        self._inner = self._factory(self._state)
        self._inner.__enter__()
        self.visits += 1
        return self

    def __exit__(self, *exc):
        inner, self._inner = self._inner, None
        if inner is not None:
            inner.__exit__(*exc)
            self._state = getattr(inner, "state", self._state)
            self._shape_errors += getattr(inner, "shape_errors", 0)

    @property
    def state(self):
        return getattr(self._inner, "state", self._state) if self._inner is not None else self._state

    def __getattr__(self, name):
        if name.startswith("_") or self.__dict__.get("_inner") is None:
            raise AttributeError(name)
        attr = getattr(self._inner, name)       # AttributeError if this searcher hasn't got it
        if name in ("search", "calendar"):      # page loads: counted towards the visit
            def counted(*a, **kw):
                self._tick()
                return getattr(self._inner, name)(*a, **kw)
            return counted
        return attr

    @property
    def shape_errors(self):
        return self._shape_errors + getattr(self._inner, "shape_errors", 0)

    def _tick(self):
        """Before a page load: start a new visit first if this one has had its share. A visit
        with only a few searches left just finishes them, as anyone would."""
        if (self._quota is not None and self._count >= self._quota and self._remaining() > 3
                and not self._worker._stop(self._uid)):
            if self._single:   # a habitual check-in is one visit: stop here, the rest is still due
                self._worker.paused.add(self._uid)
                self._worker.phase(self._uid, "This visit's done; the rest continues at the next check-in")
                raise VisitOver()
            self.__exit__(None, None, None)
            lo, hi = settings.visit_break
            pause = self._rng.uniform(lo, hi) * 60
            back = dt.datetime.now() + dt.timedelta(seconds=pause)
            self._worker.phase(self._uid, f"Taking a break between visits; back around {back:%H:%M}",
                               until=time.time() + pause)
            self._worker._pause(self._uid, pause)
            self._new_quota()
            self.__enter__()
            if not self._worker._stop(self._uid):
                self._worker.phase(self._uid, "Back on southwest.com for another visit")
        self._count += 1



ALERT_LIMIT = 5      # fares per watch in one email
BEST_KEY = "__best__"   # alerted row holding the cheapest cash and points emailed so far per watch


def _beats(r, best):
    return (r["cash"] is not None and (best["cash"] is None or r["cash"] < best["cash"])) or (
        r["points"] is not None and (best["points"] is None or r["points"] < best["points"]))


def pick_alerts(con, watch_id, cands, limit=ALERT_LIMIT):
    """What to email for a watch: on its first emailed search, the cheapest matches; after that,
    only matches cheaper (in cash or in points) than anything already emailed for it.
    Up to `limit` cheapest by cash, plus the cheapest by points if that's a different one."""
    best = con.execute("SELECT cash, points FROM alerted WHERE watch_id=? AND key=?",
                       (watch_id, BEST_KEY)).fetchone()
    if best is not None:
        cands = [c for c in cands if _beats(c["row"], best)]
    cands = sorted(cands, key=lambda c: swa.cheapest_first(c["row"]))
    out = cands[:limit]
    by_points = min((c for c in cands if c["row"]["points"] is not None),
                    key=lambda c: c["row"]["points"], default=None)
    if by_points is not None and all(by_points is not c for c in out):
        out.append(by_points)
    for c in out:   # for the email: how much cheaper than the best already sent
        c["drop"] = price_drop(c["row"], best)
    return out


def price_drop(row, best):
    """'↓ $48 / 3,000 pts since the last alert' parts, or None on a watch's first alert."""
    if best is None:
        return None
    parts = []
    if row["cash"] is not None and best["cash"] is not None and row["cash"] < best["cash"]:
        parts.append(f'${best["cash"] - row["cash"]:,.0f}')
    if row["points"] is not None and best["points"] is not None and row["points"] < best["points"]:
        parts.append(f'{int(best["points"] - row["points"]):,} pts')
    return " / ".join(parts) or None


def lowest_seen(con, watch_id, date, before):
    """Cheapest cash fare stored for this watch and date before `before` -> (cash, ts) or None."""
    r = con.execute("SELECT cash, ts FROM fares WHERE watch_id=? AND date=? AND ts<? AND cash IS NOT NULL "
                    "AND leg='out' ORDER BY cash, ts LIMIT 1", (watch_id, date, before)).fetchone()
    return (r["cash"], r["ts"]) if r else None


def rebook_key(b):
    return f'rebook:{b["leg"]}:{b["date"]}'


def rebook_hits(con, w, booked, legs):
    """Alerts for booked flights that got cheaper than what was paid (and than the last rebook alert)."""
    out = []
    for b in booked:
        offer = swa.rebook_offer(b, legs[b["leg"]].get(b["date"], []))
        if not offer:
            continue
        r = offer["row"]
        prev = con.execute("SELECT cash, points FROM alerted WHERE watch_id=? AND key=?",
                           (w["id"], rebook_key(b))).fetchone()
        if prev is not None and not _beats(r, prev):
            continue
        save = " / ".join(x for x in (f'${offer["save_cash"]:,.0f}' if offer["save_cash"] >= 1 else "",
                                      f'{int(offer["save_points"]):,} pts' if offer["save_points"] >= 1 else "") if x)
        paid = " / ".join(x for x in (f'${b["cash"]:,.0f}' if b.get("cash") is not None else "",
                                      f'{int(b["points"]):,} pts' if b.get("points") is not None else "") if x)
        frm, to = r.get("origin") or w["origin"], r.get("dest") or w["destination"]
        out.append({
            "watch_id": w["id"], "row": r, "date": b["date"], "rebook": rebook_key(b),
            "watch": f'{w["name"]}: your booked {"return" if b["leg"] == "ret" else "flight"} got cheaper',
            "use": f"Rebook and save {save} (you paid {paid})",
            "url": swa.booking_url(frm, to, b["date"], w["adults"]),
        })
    return out


def record_alerted(con, hits):
    """Lower each watch's emailed-best to include what was just sent."""
    for h in hits:
        if h.get("rebook"):
            con.execute("INSERT OR REPLACE INTO alerted VALUES (?,?,?,?)",
                        (h["watch_id"], h["rebook"], h["row"]["cash"], h["row"]["points"]))
    hits = [h for h in hits if not h.get("rebook")]
    for wid in {h["watch_id"] for h in hits}:
        row = con.execute("SELECT cash, points FROM alerted WHERE watch_id=? AND key=?",
                          (wid, BEST_KEY)).fetchone()
        cash = [h["row"]["cash"] for h in hits if h["watch_id"] == wid and h["row"]["cash"] is not None]
        pts = [h["row"]["points"] for h in hits if h["watch_id"] == wid and h["row"]["points"] is not None]
        if row is not None:
            cash += [row["cash"]] if row["cash"] is not None else []
            pts += [row["points"]] if row["points"] is not None else []
        con.execute("INSERT OR REPLACE INTO alerted VALUES (?,?,?,?)",
                    (wid, BEST_KEY, min(cash, default=None), min(pts, default=None)))


class Worker:
    def __init__(self, searcher_factory=swa.BrowserSearcher, sleep=time.sleep):
        self.searcher_factory = searcher_factory
        self.sleep = sleep
        self.q = queue.Queue()
        self.lock = threading.Lock()
        self.pending = {}          # user_id -> set(watch_ids) or None (all)
        self.signin_only = set()   # users queued just to test sign-in (no searches)
        self.running_user = None
        self.progress = {}         # user_id -> what the running search is doing right now
        self.next_window = None    # (start, end) of the next daily window
        self.planned = {}          # user_id -> start time within today's window
        self.cancelled = set()     # users who pressed Cancel: stop after the current search, keep results
        self._wake = threading.Event()   # cuts a pause short when the running user cancels
        self._threads = []
        self.seat_busy = set()     # users whose seat selection is running right now
        self.one_visit = set()     # queued for a habitual check-in: one visit, the rest continues later
        self.paused = set()        # running users whose one visit is used up
        self.started_at = dt.datetime.now()

    # ----- queueing -----
    def enqueue(self, user_id, watch_ids=None, one_visit=False):
        with self.lock:
            if one_visit and watch_ids is None:
                self.one_visit.add(user_id)
            elif user_id not in self.pending:
                self.one_visit.discard(user_id)
            if user_id in self.pending:
                cur = self.pending[user_id]
                self.pending[user_id] = None if (cur is None or watch_ids is None) else cur | set(watch_ids)
                self.signin_only.discard(user_id)
                return
            self.pending[user_id] = None if watch_ids is None else set(watch_ids)
        self.q.put(user_id)

    def enqueue_signin(self, user_id):
        """Queue a sign-in check for one user without running searches."""
        with self.lock:
            if user_id in self.pending:
                return
            self.pending[user_id] = set()
            self.signin_only.add(user_id)
        self.q.put(user_id)

    def cancel(self, user_id):
        """Stop this user's run after the search in flight (results so far are kept), or drop it
        from the queue. Returns what was cancelled: 'running', 'queued' or None."""
        state = self.status(user_id)
        if state:
            with self.lock:
                self.cancelled.add(user_id)
            if state == "running":
                self._wake.set()
                self.phase(user_id, "Cancelling: finishing the current search and saving results")
        return state

    def _stop(self, user_id):
        return user_id in self.cancelled or user_id in self.paused

    def _pause(self, user_id, seconds, searcher=None):
        """Pause between searches; a real pause ends early on Cancel. With a real clock the browser
        isn't left frozen: the person fidgets now and then."""
        if self._stop(user_id):
            return
        if self.sleep is not time.sleep:
            self.sleep(seconds)
            return
        idle = getattr(searcher, "idle", None)
        if idle:
            try:
                idle(seconds, stop=lambda: self._stop(user_id) or self._wake.is_set())
                return
            except Exception as e:
                applog.warn(f"Idle activity failed ({e.__class__.__name__}); waiting quietly",
                            user_id=user_id, source="browser")
        self._wake.wait(seconds)

    # ----- live progress (shown in the UI while a run is going) -----
    def phase(self, user_id, text, watch_id=None, done=None, total=None, log=True, level="info", source="run",
              until=None):
        """until: epoch seconds a pause ends, so the page can count down."""
        p = self.progress.setdefault(user_id, {"started": db.now()})
        p.update(phase=text, at=db.now(), until=until)
        if watch_id is not None:
            p["watch_id"] = watch_id
        if total is not None:
            p["done"], p["total"] = done or 0, total
        if log:
            applog.log(text, level, user_id=user_id, watch_id=watch_id or p.get("watch_id"), source=source)

    def snapshot(self, user_id):
        """JSON-friendly view of this user's run for the live status display."""
        state = self.status(user_id)
        p = dict(self.progress.get(user_id) or {})
        return {"state": state, **({k: p.get(k) for k in ("phase", "done", "total", "watch_id", "started", "at", "until")}
                                   if state == "running" else {})}

    def status(self, user_id):
        if self.running_user == user_id:
            return "running"
        return "queued" if user_id in self.pending else None

    def eligible_users(self):
        rows = db.query("SELECT DISTINCT u.* FROM users u JOIN watches w ON w.user_id=u.id "
                        "WHERE w.active=1 AND u.active=1 AND w.kind IN ('fare','rebook')")
        now = db.now()
        return [u["id"] for u in rows if db.user_can_run(u) and not (u["blocked_until"] and u["blocked_until"] > now)]

    def enqueue_all(self):
        """Queue everyone now (admin 'Run all now'), in a shuffled order."""
        ids = self.eligible_users()
        random.shuffle(ids)
        for uid in ids:
            self.enqueue(uid)
        return len(ids)

    @staticmethod
    def close_interrupted_runs():
        """Runs left unfinished by a crash or restart would show "Running…" forever."""
        with db.tx() as con:
            n = con.execute("UPDATE runs SET finished=?, note='Interrupted (the app restarted mid-run)' "
                            "WHERE finished IS NULL", (db.now(),)).rowcount
            con.execute("UPDATE watches SET last_status='Interrupted (the app restarted mid-run)' "
                        "WHERE last_status='Running…'")
        if n:
            applog.warn(f"Marked {n} unfinished run(s) from before the restart as interrupted", source="run")

    def start(self, scheduler=True):
        self.close_interrupted_runs()
        t = threading.Thread(target=self._loop, name="swa-worker", daemon=True)
        t.start()
        self._threads.append(t)
        h = threading.Thread(target=self._housekeeping_loop, name="swa-housekeeping", daemon=True)
        h.start()
        self._threads.append(h)
        st = threading.Thread(target=self._seat_loop, name="swa-seats", daemon=True)
        st.start()
        self._threads.append(st)
        if scheduler:
            s = threading.Thread(target=self._schedule, name="swa-scheduler", daemon=True)
            s.start()
            self._threads.append(s)

    def _loop(self):
        while True:
            uid = self.q.get()
            with self.lock:
                ids = self.pending.pop(uid, None)
                only = uid in self.signin_only
                self.signin_only.discard(uid)
                single = uid in self.one_visit and ids is None
                self.one_visit.discard(uid)
                if uid in self.cancelled:   # cancelled while queued
                    self.cancelled.discard(uid)
                    continue
                self.running_user = uid
                self._wake.clear()
            while uid in self.seat_busy:      # its seat selection is using the browser profile
                time.sleep(5)
            try:
                self.run_user(uid, ids, signin_only=only, one_visit=single)
            except Exception as e:
                if not getattr(e, "logged", False):
                    applog.error(f"Run stopped by an error: {e.__class__.__name__}: {e}", user_id=uid,
                                 source="run", exc=True)
            finally:
                with self.lock:
                    self.running_user = None
                    self.cancelled.discard(uid)
                    self.paused.discard(uid)
                self.progress.pop(uid, None)

    # ----- seat selection (48 h before departure)
    SEAT_EARLY = 150          # be on the seat map this many seconds before selection opens
    SEAT_WINDOW = 25 * 60     # and keep checking for this long after

    def due_seat_jobs(self, now=None):
        now = now or dt.datetime.now(dt.timezone.utc)
        soon = (now + dt.timedelta(seconds=self.SEAT_EARLY)).isoformat(timespec="seconds")
        return db.query("SELECT j.*, w.user_id FROM seat_jobs j JOIN watches w ON w.id=j.watch_id "
                        "WHERE j.status='pending' AND j.opens_at<=? AND w.active=1 AND w.seat_auto=1", (soon,))

    def _seat_loop(self):
        while True:
            try:
                for job in self.due_seat_jobs():
                    with db.tx() as con:   # claim it
                        if con.execute("UPDATE seat_jobs SET status='running', updated=? WHERE id=? "
                                       "AND status='pending'", (db.now(), job["id"])).rowcount != 1:
                            continue
                    threading.Thread(target=self.run_seat_job, args=(dict(job),), name="swa-seat-job",
                                     daemon=True).start()
            except Exception as e:
                applog.error(f"Seat scheduler: {e.__class__.__name__}: {e}", source="seats")
            time.sleep(20)

    def run_seat_job(self, job, picker_factory=None):
        """Open the booking just before seat selection opens and move each passenger to the best
        free seat that beats theirs. Emails the result either way."""
        from . import seat_picker
        w = db.one("SELECT * FROM watches WHERE id=?", (job["watch_id"],))
        user = db.one("SELECT * FROM users WHERE id=?", (w["user_id"],)) if w else None
        if not w or not user:
            return None
        uid = user["id"]
        leg = "return" if job["leg"] == "ret" else "outbound"
        opens = dt.datetime.fromisoformat(job["opens_at"])
        applog.info(f"{w['name']}: seat selection for the {leg} flight opens at "
                    f"{opens.astimezone():%H:%M}; opening the booking", user_id=uid, source="seats")
        status, changes, note = "failed", [], ""
        same_profile = self.running_user != uid
        self.seat_busy.add(uid)
        try:
            factory = picker_factory or (lambda: self.searcher_factory(
                None, profile_key=uid if same_profile else f"{uid}-seats"))
            with factory() as browser:
                picker = seat_picker.SeatPicker(browser, w["booking_conf"], w["booking_first"], w["booking_last"],
                                                seats.prefs_of(w), w["seat_status"] or "none", user_id=uid)
                until = max(opens.timestamp(), time.time()) + self.SEAT_WINDOW
                changes, note = picker.run(until, sleep=self.sleep)
                status = "done" if changes else "unchanged"
        except seat_picker.NotFree as e:
            note = f"Stopped before paying anything: {e}. Your seat wasn't changed."
        except swa.Blocked as e:
            note = f"Southwest blocked the page ({e}). Pick your seat yourself."
        except Exception as e:
            note = f"Couldn't finish ({e.__class__.__name__}: {str(e)[:200]}). Pick your seat yourself."
        finally:
            self.seat_busy.discard(uid)
        if status == "unchanged":
            note = "Your current seat is already the best free match, so it wasn't changed."
        elif status == "done":
            note = f"Seat change {note}."
        with db.tx() as con:
            con.execute("UPDATE seat_jobs SET status=?, note=?, updated=? WHERE id=?",
                        (status, note, db.now(), job["id"]))
        applog.info(f"{w['name']}: seat selection ({leg}): {status}. {note}", user_id=uid, source="seats")
        if user["email"]:
            subject = {"done": "Seat selected", "unchanged": "Seat checked, no change",
                       "failed": "Seat selection needs you"}[status]
            mailer.send(user["email"], f"SWA {subject}: {w['name']} ({leg})",
                        mailer.render_seat_result(f"{w['name']}: {leg} flight", changes, note,
                                                  status != "failed", seat_picker.MANAGE_URL))
        return status, changes, note

    def housekeeping(self):
        """Background chores, checked every few minutes; each is safe to repeat."""
        try:
            made = db.backup()
            if made:
                applog.info(f"Database backed up to {made.relative_to(settings.data_dir)}", source="backup")
        except Exception as e:
            applog.error(f"Database backup failed: {e.__class__.__name__}: {e}", source="backup")
        try:
            self.send_digests()
        except Exception as e:
            applog.error(f"Digest emails failed: {e.__class__.__name__}: {e}", source="digest")
        try:
            self.check_stale()
        except Exception as e:
            applog.error(f"Stale-run check failed: {e.__class__.__name__}: {e}", source="scheduler")

    @staticmethod
    def send_digests(now=None):
        """Email each daily/weekly user their saved alerts once settings.digest_at has passed and a
        day (or week) has gone by since the last digest. Keeps only the newest alert per trip."""
        now = now or dt.datetime.now()
        h, m = (int(x) for x in settings.digest_at.split(":"))
        sent = 0
        for u in db.query("SELECT u.* FROM users u WHERE u.active=1 "
                          "AND EXISTS (SELECT 1 FROM digest_hits d WHERE d.user_id=u.id)"):
            mode = u["alert_mode"] or "instant"
            if mode != "instant":   # switched back to instant: send what's waiting right away
                if now.time() < dt.time(h, m):
                    continue
                gap = dt.timedelta(days=7 if mode == "weekly" else 1) - dt.timedelta(hours=2)
                if u["digest_sent_at"] and now - dt.datetime.fromisoformat(u["digest_sent_at"]) < gap:
                    continue
            rows = db.query("SELECT id, hit FROM digest_hits WHERE user_id=? ORDER BY id", (u["id"],))
            latest = {}
            for r in rows:
                hit = json.loads(r["hit"])
                t = hit.get("trip") or {}
                latest[(hit["watch_id"], hit.get("date"), t.get("ret_date"), hit.get("watch"))] = hit
            hits = sorted(latest.values(), key=lambda x: (x["watch"], x.get("date") or ""))
            period = "week" if mode == "weekly" else "day"
            if u["email"] and mailer.send(u["email"], f"SWA fare digest: {len(hits)} deal(s) this {period}",
                                          mailer.render_hits(hits)):
                with db.tx() as con:
                    con.execute(f"DELETE FROM digest_hits WHERE user_id=? AND id<={rows[-1]['id']}", (u["id"],))
                    con.execute("UPDATE users SET digest_sent_at=? WHERE id=?",
                                (now.isoformat(timespec="seconds"), u["id"]))
                applog.info(f"Digest emailed: {len(hits)} deal(s)", user_id=u["id"], source="digest")
                sent += 1
        return sent

    @staticmethod
    def check_gpu(searcher, day=None):
        """Once a day: if pages see software rendering (SwiftShader) instead of the GPU, warn the
        admins by email with the reason (SWA_GPU off, device not passed in, or no permission)."""
        if not hasattr(searcher, "renderer"):
            return None
        day = day or db.today()
        if db.get_state("gpu_check_day") == day:
            return None
        gl = searcher.renderer()
        db.set_state("gpu_check_day", day)
        if gl and "swiftshader" not in gl.lower():
            applog.info(f"Rendering with the GPU: {gl}", source="browser")
            return True
        why = swa.gpu_problem() or "SWA_GPU is on and the device is usable, but Chrome still fell back " \
            "(check the GPU drivers in the image and the browser log)"
        msg = (f"Chrome is drawing pages in software ({gl or 'no WebGL'}), which sites can recognise as a "
               f"machine without a GPU. Reason: {why}.")
        applog.warn(msg, source="browser")
        for to in db.admin_emails():
            mailer.send(to, "SWA fare watcher: GPU rendering is off", f"<p>{html.escape(msg)}</p>")
        return False

    @staticmethod
    def check_format(attempts, shape_errors, priceless, day=None):
        """If most of a run's searches came back in a shape the parser doesn't know, or with flights
        but no prices, Southwest has likely changed its data: warn the admins (once a day)."""
        odd = shape_errors + priceless
        if attempts < 3 or odd < 3 or odd * 2 < attempts:
            return False
        day = day or db.today()
        if db.get_state("format_alert_day") == day:
            return True
        msg = (f"{odd} of {attempts} fare searches in one run came back in a shape the app doesn't "
               f"understand ({shape_errors} unrecognised, {priceless} with flights but no prices). "
               "Southwest has probably changed its site; fares will be missing until the parser is updated. "
               "Raw responses are saved under debug/.")
        applog.error(msg, source="search")
        for to in db.admin_emails():
            mailer.send(to, "SWA fare watcher: Southwest changed its fare data", f"<p>{html.escape(msg)}</p>")
        db.set_state("format_alert_day", day)
        return True

    def alive(self):
        """False if a background thread this worker started has died."""
        return all(t.is_alive() for t in self._threads)

    def check_stale(self, now=None):
        """Email the admins once when no run has finished for settings.stale_hours while there are
        watches to run; reset when runs finish again."""
        if not settings.stale_hours or not settings.scheduler:
            return None
        now = now or dt.datetime.now()
        last = db.one("SELECT MAX(finished) f FROM runs")["f"]
        since = max(filter(None, [dt.datetime.fromisoformat(last) if last else None, self.started_at]))
        flagged = db.get_state("stale_alerted")
        if now - since < dt.timedelta(hours=settings.stale_hours) or not self.eligible_users():
            if flagged:
                db.set_state("stale_alerted", "")
            return False
        if flagged:
            return True
        hours = (now - since).total_seconds() / 3600
        msg = (f"No fare search has finished for {hours:.0f} hours (last: {last or 'never'}). "
               "Check the app's Log tab and that the container and browser are running.")
        applog.warn(msg, source="scheduler")
        for to in db.admin_emails():
            mailer.send(to, "SWA fare watcher: no searches have run",
                        f"<p>{html.escape(msg)}</p><p><a href='{settings.base_url}/log'>Open the log</a></p>")
        db.set_state("stale_alerted", now.isoformat(timespec="seconds"))
        return True

    def _housekeeping_loop(self):
        while True:
            self.housekeeping()
            time.sleep(600)

    def _sleep_until(self, when):
        while (wait := (when - dt.datetime.now()).total_seconds()) > 0:
            time.sleep(min(wait, 300))  # re-check: survives clock changes and sleep/suspend

    def _schedule(self):
        if settings.habit_slots:
            return self._schedule_slots()
        last_day = None
        while True:
            spread = settings.user_spread_min
            day, lo, hi = day_window(settings.daily_at, settings.daily_jitter_min + spread, last_day=last_day)
            self.next_window = (lo, hi)
            applog.info(f"Next daily run window {lo:%a %Y-%m-%d %H:%M}-{hi:%H:%M}", source="scheduler")
            self._sleep_until(lo)
            users = self.eligible_users()
            skipped = [u for u in users if skip_today(u, day, settings.skip_day_chance)]
            plan = plan_users([u for u in users if u not in skipped], day, dt.datetime.now(), settings.daily_at,
                              spread, settings.daily_jitter_min)
            self.planned = {uid: t for t, uid in plan}
            applog.info(f"Today's plan, {len(plan)} user(s): "
                        + (", ".join(f"user {uid} at {t:%H:%M:%S}" for t, uid in plan) or "nobody to run")
                        + (f"; taking the day off: {', '.join(f'user {u}' for u in skipped)}" if skipped else ""),
                        source="scheduler")
            for t, uid in plan:
                self._sleep_until(t)
                self.enqueue(uid)
                self.planned.pop(uid, None)
            last_day = day

    def _schedule_slots(self):
        """Each day: every user's habitual check-in times; at each one, queue a one-visit run."""
        last_day = None
        while True:
            now = dt.datetime.now()
            day = now.date() if last_day != now.date() else now.date() + dt.timedelta(days=1)
            if day > now.date():
                self.next_window = (dt.datetime.combine(day, dt.time(0, 5)), None)
                self._sleep_until(dt.datetime.combine(day, dt.time(0, 5)))
            users = self.eligible_users()
            skipped = [u for u in users if skip_today(u, day, settings.skip_day_chance)]
            plan = plan_slots([u for u in users if u not in skipped], day, dt.datetime.now())
            self.planned = {}
            for t, uid in plan:
                self.planned.setdefault(uid, t)
            applog.info(f"Today's check-ins, {len(plan)}: "
                        + (", ".join(f"user {uid} at {t:%H:%M}" for t, uid in plan) or "none")
                        + (f"; taking the day off: {', '.join(f'user {u}' for u in skipped)}" if skipped else ""),
                        source="scheduler")
            for i, (t, uid) in enumerate(plan):
                self._sleep_until(t)
                self.enqueue(uid, one_visit=True)
                later = [x for x, u in plan[i + 1:] if u == uid]
                if later:
                    self.planned[uid] = later[0]
                else:
                    self.planned.pop(uid, None)
            last_day = day

    def next_run_for(self, user_id):
        """This user's planned start today, or the next window (start, end)."""
        if user_id in self.planned:
            return self.planned[user_id], None
        return None, self.next_window

    # ----- the actual run -----
    def run_user(self, user_id, watch_ids=None, signin_only=False, one_visit=False):
        user = db.one("SELECT * FROM users WHERE id=?", (user_id,))
        if not user or not db.user_can_run(user):
            return {"skipped": "user inactive"}
        creds = decrypt_json(user["swa_creds"]) if (settings.auto_login and user["auto_login"]) else None
        watches = [] if signin_only else db.query(
            "SELECT * FROM watches WHERE user_id=? AND active=1 AND kind IN ('fare','rebook') ORDER BY id",
            (user_id,))
        if watch_ids is not None:
            watches = [w for w in watches if w["id"] in watch_ids]
        if not watches and not (signin_only and creds):
            return {"skipped": "no active watches" if not signin_only else "automatic sign-in not set up"}

        state = decrypt_json(user["swa_state"])
        cap = user["max_searches_per_day"]
        hits, attempts, failures = [], 0, 0
        priceless = 0   # searches whose flights had no price at all (data-format warning sign)
        limited = False
        signin = None

        watches = random.sample(watches, len(watches))  # different watch order each day
        plan = [(w, swa.search_legs(w)) for w in watches]
        scheduled = watch_ids is None and not signin_only   # Search now always searches everything
        if scheduled and settings.rotate:
            plan = [(w, due_legs(w, legs)) for w, legs in plan]
        budget = page_budget(user_id) if scheduled else None
        if budget is not None:
            room = max(0, budget - db.searches_today(user_id))
            plan = fit_budget(plan, room)
            cap = min(cap, budget) if cap is not None else budget
        plan = [(w, search_order(legs)) for w, legs in plan if legs or (w["kind"] or "fare") == "rebook"]
        plan = [(w, legs) for w, legs in plan if legs]  # [(leg, date)], legs mixed
        if not plan and not signin_only:
            applog.info("Nothing due today: every date was searched recently or today's page budget is used",
                        user_id=user_id, source="run")
            return {"skipped": "nothing due today"}
        total = sum(2 * len(swa.routes(w)) * len(d) for w, d in plan)
        done = 0
        self.phase(user_id, "Sign-in check only" if signin_only else
                   f"Starting a run: {len(plan)} watch(es), up to {total} searches", done=0, total=total)
        open_runs = {}  # watch_id -> run_id not yet finished (closed even if something breaks)
        searcher = None
        try:
            self.phase(user_id, "Starting the browser")
            visits = Visits(self, user_id, lambda st: self.searcher_factory(st, profile_key=user_id), state,
                            remaining=lambda: total - done, single=one_visit)
            with visits as searcher:
                self.check_gpu(searcher)
                try:
                    if creds and hasattr(searcher, "ensure_signed_in"):
                        self.phase(user_id, "Opening southwest.com and checking you're signed in")
                        signin = self._sign_in(user, creds, searcher, manual=signin_only)
                        if signin.status == "blocked":
                            raise swa.Blocked("sign-in", signin.detail or "blocked", signin.screenshot)
                    elif plan:
                        self.phase(user_id, "Opening southwest.com")
                    for w, dates in plan:
                        if self._stop(user_id):
                            break
                        route = f'{w["name"]} ({swa.route_label(w)})'
                        planned = set(dates)
                        w_started = db.now()
                        with db.tx() as con:
                            open_runs[w["id"]] = con.execute("INSERT INTO runs(watch_id, started) VALUES (?,?)",
                                                             (w["id"], w_started)).lastrowid
                        with db.tx() as con:
                            con.execute("UPDATE watches SET last_status=? WHERE id=?", ("Running…", w["id"]))
                        rt = swa.is_roundtrip(w)
                        n_out = sum(1 for leg, _ in dates if leg == "out")
                        self.phase(user_id, f"{route}: {n_out} outbound" + (f" and {len(dates) - n_out} return"
                                   if rt else "") + " date(s) to search", watch_id=w["id"])
                        w_s = w_f = w_m = 0
                        note = None
                        n_routes = len(swa.routes(w))
                        to_search = {}   # (leg, date) -> airport pairs; default: all of them
                        fare = (w["kind"] or "fare") == "fare"
                        if fare and settings.calendar and hasattr(searcher, "calendar") and dates:
                            before = 2 * n_routes * len(dates)
                            dates, to_search, used, hit_cap = self._calendar_phase(
                                user_id, w, route, dates, searcher, cap)
                            w_s += used
                            total += used + 2 * sum(len(v) for v in to_search.values()) - before
                            done += used
                            self.phase(user_id, f"{route}: calendar narrowed it to {len(dates)} date(s) to search",
                                       watch_id=w["id"], done=done, total=total)
                            if hit_cap:
                                note, limited, dates = "Daily search limit reached", True, []
                            elif not dates:
                                note = "No date on the calendar under your target"
                        # dates of flights already booked are always searched (rebook check)
                        booked = [b for b in swa.booked(w) if b["date"] >= db.today() and (rt or b["leg"] == "out")]
                        want = list(dict.fromkeys((b["leg"], b["date"]) for b in booked))
                        extra = set(want) - planned
                        if not limited:
                            dates = list(dates) + [k for k in want if k not in set(dates)]
                        legs = {"out": {}, "ret": {}}  # this run's flights per leg and date
                        multi = n_routes > 1
                        cands = []   # this watch's matching flights or round trips; pick_alerts decides
                        for leg, date in dates:
                            ts = db.now()   # one timestamp per date, shared by its airport pairs
                            date_start = attempts
                            partial = False   # only one currency read before the visit ended
                            for frm, to in to_search.get((leg, date), swa.routes(w, leg)):
                                if self._stop(user_id):
                                    break
                                if cap is not None and db.searches_today(user_id) + 2 > cap:
                                    note, limited = "Daily search limit reached", True
                                    self.phase(user_id, f"{route}: daily search limit reached, stopping",
                                               watch_id=w["id"], level="warn")
                                    break
                                res = {}
                                order = random.sample(("USD", "POINTS"), 2)   # cash or points first
                                shown = getattr(searcher, "shown_currency", lambda *a: None)(frm, to, w["adults"])
                                if shown in order:   # results already on screen: next date is a date-strip click
                                    order = [shown, "POINTS" if shown == "USD" else "USD"]
                                for ft in order:
                                    if res and self._stop(user_id):
                                        break   # keep the currency already read
                                    done += 1
                                    self.phase(user_id, f"{route}: searching {frm}→{to} {date} "
                                               f"({'cash' if ft == 'USD' else 'points'})",
                                               watch_id=w["id"], done=done, total=total)
                                    try:
                                        res[ft] = searcher.search(frm, to, date, w["adults"], ft)
                                    except VisitOver:   # this check-in's visit is used up
                                        done -= 1
                                        partial = partial or bool(res)
                                        break
                                    with db.tx() as con:
                                        db.add_searches(con, user_id, 1)
                                    attempts += 1
                                    w_s += 1
                                    if swa.priceless(res[ft]):
                                        priceless += 1
                                    if res[ft] is None:
                                        failures += 1
                                        w_f += 1
                                        self.phase(user_id, f"{route}: {frm}→{to} {date} {ft.lower()} search "
                                                   "returned no fares", watch_id=w["id"], level="warn")
                                    no_flights = res[ft] == {} and len(res) == 1
                                    if no_flights:   # nothing flies that day: the other currency won't either
                                        res[order[1]] = {}
                                        self.phase(user_id, f"{route}: no flights {frm}→{to} on {date}",
                                                   watch_id=w["id"])
                                    pause = search_pause(*settings.search_delay)
                                    self.phase(user_id, f"Pausing {pause:.0f} s before the next search",
                                               watch_id=w["id"], log=False, until=time.time() + pause)
                                    self._pause(user_id, pause, searcher)
                                    if no_flights:
                                        break
                                n_flights = n_match = 0
                                rows = swa.combine(res.get("USD"), res.get("POINTS"))
                                for r in rows:
                                    r["origin"], r["dest"] = frm, to
                                legs[leg].setdefault(date, []).extend(rows)
                                with db.tx() as con:
                                    for r in rows:
                                        n_flights += 1
                                        # one-way: the flight must match on its own; round trip: totals decide below
                                        matched = swa.passes(r, w) if not rt else swa.passes_leg(r, w, leg)
                                        con.execute(
                                            "INSERT INTO fares(watch_id, ts, date, leg, origin, dest, flight_key, flights,"
                                            " depart, arrive, stops, via, duration, cash, points, points_taxes, cpp, matched)"
                                            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                            (w["id"], ts, date, leg, frm, to, r["key"], r["flights"], r["depart"],
                                             r["arrive"], r["stops"], r.get("via") or "", r["duration"], r["cash"],
                                             r["points"], r["points_taxes"], r["cpp"], int(matched)))
                                        if not matched or rt or (leg, date) in extra or not fare:
                                            continue
                                        w_m += 1
                                        n_match += 1
                                        cands.append({
                                            "watch_id": w["id"], "row": r, "date": date,
                                            "watch": f'{w["name"]} ({frm}→{to})', "use": swa.recommend_detail(r, w),
                                            "url": swa.booking_url(frm, to, date, w["adults"]),
                                        })
                                cheap_c = min((r["cash"] for r in rows if r["cash"] is not None), default=None)
                                cheap_p = min((r["points"] for r in rows if r["points"] is not None), default=None)
                                cheapest = ", cheapest " + " / ".join(
                                    x for x in (f"${cheap_c:,.0f}" if cheap_c is not None else "",
                                                f"{cheap_p:,.0f} pts" if cheap_p is not None else "") if x) \
                                    if (cheap_c is not None or cheap_p is not None) else ""
                                applog.info(f"{route}: {frm}→{to} {date}: {n_flights} flight(s)" + cheapest
                                            + ("" if rt else f", {n_match} matching"),
                                            user_id=user_id, watch_id=w["id"], source="run")
                            if attempts > date_start and not partial:
                                with db.tx() as con:
                                    record_searched(con, w["id"], leg, date, legs[leg].get(date, []))
                            if limited or self._stop(user_id):
                                break
                        if rt and fare:
                            # pair with directions searched at earlier check-ins too (rotation keeps
                            # those at most a few days old); this run's prices win
                            since = (dt.datetime.now() - dt.timedelta(days=5)).isoformat(timespec="seconds")
                            pool = db.latest_legs(w["id"], since=since) if one_visit or settings.rotate else \
                                {"out": {}, "ret": {}}
                            valid = set(swa.search_legs(w))
                            pool = {lg: {d: v for d, v in by.items() if (lg, d) in valid} for lg, by in pool.items()}
                            for lg, by in legs.items():
                                pool.setdefault(lg, {}).update(by)
                            trips = swa.roundtrips(w, {lg: {d: v for d, v in by.items() if (lg, d) not in extra}
                                                       for lg, by in pool.items()})
                            for t in trips:
                                if not t["matched"]:
                                    continue
                                w_m += 1
                                cands.append({
                                    "watch_id": w["id"], "trip": t, "date": t["out_date"],
                                    "row": {"cash": t["cash"], "points": t["points"]},
                                    "watch": f'{w["name"]} ({t["out_route"]}, {t["ret_route"]})'
                                             if multi else route, "use": t["use_detail"],
                                    "url": t["url"], "ret_url": t["ret_url"],
                                })
                            applog.info(f"{route}: {len(trips)} round trip(s) priced, {w_m} matching",
                                        user_id=user_id, watch_id=w["id"], source="run")
                        with db.tx() as con:
                            hits.extend(rebook_hits(con, w, booked, legs))
                            new = pick_alerts(con, w["id"], cands, 1 if swa.best_only(w) else ALERT_LIMIT)
                            for c in new:
                                if not c.get("trip"):
                                    c["low"] = lowest_seen(con, w["id"], c["date"], w_started)
                        if cands and not new:
                            applog.info(f"{route}: nothing cheaper than the fares already emailed",
                                        user_id=user_id, watch_id=w["id"], source="run")
                        hits.extend(new)
                        if user_id in self.paused:
                            note = f"{w_s} search(es) this visit, {w_m} matching; the rest at the next check-in"
                        elif self._stop(user_id):
                            note = f"Cancelled after {w_s} search(es), {w_m} matching"
                        if note is None:
                            note = ("All searches failed" if w_s and w_f == w_s
                                    else f"{w_m} matching {'round trip' if rt else 'flight'}(s)" if w_s
                                    else "No upcoming dates")
                        self._finish_run(open_runs.pop(w["id"]), w["id"], note, w_s, w_f, w_m)
                        self.phase(user_id, f"{route}: done, {note.lower()}", watch_id=w["id"])
                    self.phase(user_id, "Closing the browser")
                except Exception as e:
                    # what the page looked like when it broke, before the browser closes
                    e.screenshot = searcher.capture("error") if hasattr(searcher, "capture") else None
                    raise
        except Exception as e:
            reason = f"Error: {e.__class__.__name__}: {str(e).splitlines()[0][:160] if str(e) else ''}".rstrip(": ")
            if isinstance(e, swa.Blocked):
                reason = f"Stopped: Southwest blocked the {e.where} ({e.reason})"
                self._pause_after_block(user, e)
            else:
                applog.error(f"Run stopped by an error: {e.__class__.__name__}: {e}", user_id=user_id,
                             source="run", exc=True, attachment=getattr(e, "screenshot", None))
            e.logged = True
            for wid, rid in open_runs.items():
                self._finish_run(rid, wid, reason, 0, 0, 0)
            if not open_runs and plan and searcher is None:  # browser never started
                with db.tx() as con:
                    for w, _ in plan:
                        con.execute("UPDATE watches SET last_run_at=?, last_status=? WHERE id=?",
                                    (db.now(), reason, w["id"]))
            raise
        new_state = searcher.state  # refreshed cookies, read after the browser context closes

        all_failed = attempts > 0 and failures == attempts
        signed_in = signin is not None and signin.ok
        with db.tx() as con:
            if (state is not None or signed_in) and new_state and not all_failed:
                con.execute("UPDATE users SET swa_state=? WHERE id=?", (encrypt_json(new_state), user_id))
            if attempts:
                status = ("Searches failed: Southwest session may have expired or the request was blocked"
                          if all_failed else "OK")
                con.execute("UPDATE users SET swa_status=?, swa_status_at=? WHERE id=?",
                            (status, db.now(), user_id))

        self.check_format(attempts, getattr(searcher, "shape_errors", 0), priceless)
        cancelled = user_id in self.cancelled
        applog.info(f"Run {'cancelled' if cancelled else 'paused until the next check-in' if user_id in self.paused else 'finished'}: {attempts} search(es), {failures} failed, {len(hits)} new or lower fare(s)"
                    + (" (daily limit reached)" if limited else ""), user_id=user_id, source="run")
        sent = False
        if hits:
            with db.tx() as con:   # look at those dates again soon, like someone weighing a deal
                mark_hot(con, hits)
        rebooks = [h for h in hits if h.get("rebook")]
        regular = [h for h in hits if not h.get("rebook")]
        if regular and (user["alert_mode"] or "instant") != "instant":
            with db.tx() as con:   # saved for the digest; counts as alerted so it isn't queued again
                for h in regular:
                    con.execute("INSERT INTO digest_hits(user_id, created, hit) VALUES (?,?,?)",
                                (user_id, db.now(), json.dumps(h, default=str)))
                record_alerted(con, regular)
            applog.info(f"{len(regular)} fare alert(s) saved for your {user['alert_mode']} digest",
                        user_id=user_id, source="run")
            hits_for_email = rebooks   # price drops on booked flights don't wait for the digest
        else:
            hits_for_email = rebooks + regular
        if hits_for_email:
            self.phase(user_id, f"Emailing {len(hits_for_email)} fare alert(s)", log=False)
            subject = ("SWA price drop on your booked flight: rebook to save" if rebooks
                       else f"SWA fare alert: {len(hits_for_email)} match(es)")
            sent = mailer.send(user["email"], subject, mailer.render_hits(hits_for_email))
            if sent:
                with db.tx() as con:
                    record_alerted(con, hits_for_email)
        if all_failed and not (creds and signin is not None and signin.status in ("success", "already_signed_in")):
            mailer.send(user["email"], "SWA fare watcher: searches failed",
                        "<p>No fare data was returned for your watches. Your Southwest session may "
                        f"have expired. Upload a fresh one under <a href='{settings.base_url}/settings'>"
                        "Settings</a>.</p>")
        out = {"attempts": attempts, "failures": failures, "hits": len(hits),
               "emailed": sent, "limited": limited, "cancelled": cancelled}
        if signin is not None:
            out["signin"] = signin.status
        return out

    def _calendar_phase(self, user_id, w, route, dates, searcher, cap):
        """Read the low-fare calendar (cash and points, for each month, direction and airport
        pair) and keep the dates and pairs worth a full search.
        Returns (dates, {(leg, date): airport pairs}, calendar loads used, hit the daily cap)."""
        cal, cal_routes, used = {}, {"out": {}, "ret": {}}, 0
        one_currency = set()
        cal_start = db.now()
        for leg in ("out", "ret"):
            leg_dates = [d for lg, d in dates if lg == leg]
            if not leg_dates:
                continue
            months = list(swa.calendar_months(leg_dates).items())
            # Per route, like a person on one calendar page: every month in one currency (month
            # tabs), switch currency on the last month, then back through the months. Routes,
            # currency order and month direction are random.
            jobs = []
            for rt_ in random.sample(swa.routes(w, leg), len(swa.routes(w, leg))):
                ms = months if random.random() < 0.5 else months[::-1]
                a, b = random.sample(("USD", "POINTS"), 2)
                jobs += [(rt_, m, first, a) for m, first in ms] + [(rt_, m, first, b) for m, first in ms[::-1]]
            lows = {rt_: {} for rt_ in swa.routes(w, leg)}
            failed = set()   # (route, currency) whose calendar couldn't be read
            for j, ((frm, to), month, first, ft) in enumerate(jobs):
                if self._stop(user_id):
                    return [], {}, used, False
                if cap is not None and db.searches_today(user_id) + 1 > cap:
                    self.phase(user_id, f"{route}: daily search limit reached, stopping",
                               watch_id=w["id"], level="warn")
                    return [], {}, used, True
                self.phase(user_id, f"{route}: reading the {month} {'cash' if ft == 'USD' else 'points'} "
                           f"calendar {frm}→{to}", watch_id=w["id"])
                try:
                    days = searcher.calendar(frm, to, first, w["adults"], ft)
                except VisitOver:
                    return [], {}, used, False
                with db.tx() as con:
                    db.add_searches(con, user_id, 1)
                used += 1
                if days is None:
                    failed.add(((frm, to), ft))
                else:
                    field = "cash" if ft == "USD" else "points"
                    for d, v in days.items():
                        lows[(frm, to)].setdefault(d, {"cash": None, "points": None})[field] = v
                nxt = jobs[j + 1] if j + 1 < len(jobs) else None
                can_click = getattr(searcher, "calendar_can_click", None)
                if nxt and can_click and can_click(nxt[0][0], nxt[0][1], nxt[2], w["adults"], nxt[3]):
                    continue   # the next calendar is a click on this page (with its own short look)
                pause = search_pause(*settings.search_delay)
                self.phase(user_id, f"Pausing {pause:.0f} s before the next search",
                           watch_id=w["id"], log=False, until=time.time() + pause)
                self._pause(user_id, pause, searcher)
            # a route is unreadable only if neither currency's calendar could be read; with one,
            # its days are still ranked by the currency that came back
            broken = {rt_ for rt_ in swa.routes(w, leg) if {(rt_, "USD"), (rt_, "POINTS")} <= failed}
            one_currency |= {ft for rt_, ft in failed if rt_ not in broken}
            ts = db.now()
            with db.tx() as con:
                con.executemany(
                    "INSERT INTO calendar_fares(watch_id, ts, leg, origin, dest, date, cash, points)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    [(w["id"], ts, leg, frm, to, d, v["cash"], v["points"])
                     for (frm, to), days in lows.items() if (frm, to) not in broken for d, v in sorted(days.items())])
            for frm, to in swa.routes(w, leg):
                if (frm, to) in broken:
                    cal_routes[leg][(frm, to)] = None
                    applog.warn(f"{route}: {frm}→{to} calendar unreadable; searching every date",
                                user_id=user_id, watch_id=w["id"], source="run")
                    continue
                cal_routes[leg][(frm, to)] = lows[(frm, to)]
                cheap = min((v["cash"] for d, v in lows[(frm, to)].items()
                             if d in leg_dates and v["cash"] is not None), default=None)
                applog.info(f"{route}: {frm}→{to} calendar read for {len(leg_dates)} date(s)"
                            + (f", lowest ${cheap:,.0f} per person" if cheap is not None else ""),
                            user_id=user_id, watch_id=w["id"], source="run")
            # per day, the cheapest over all airport pairs (any unreadable pair: keep every date)
            if broken:
                cal[leg] = None
            else:
                merged = {}
                for days in lows.values():
                    for d, v in days.items():
                        m = merged.setdefault(d, {"cash": None, "points": None})
                        for f in ("cash", "points"):
                            if v[f] is not None and (m[f] is None or v[f] < m[f]):
                                m[f] = v[f]
                cal[leg] = merged
        w_pick = w
        if one_currency:
            # one currency's calendar was unreadable, so its target can't be checked: keep the days
            # cheapest in the currency that did come back (cash and points prices move together)
            missing = " and ".join(sorted("cash" if c == "USD" else "points" for c in one_currency))
            applog.warn(f"{route}: {missing} calendar unreadable; choosing dates by the other currency's lows",
                        user_id=user_id, watch_id=w["id"], source="run")
            w_pick = {**dict(w), "max_price": None, "max_points": None}
        picked = swa.calendar_pick(w_pick, cal, dates, settings.calendar_detail_dates)
        to_search = swa.calendar_routes(w_pick, cal_routes, picked)
        picked = [x for x in picked if to_search.get(x)]
        if picked and any(cal.get(lg) for lg in ("out", "ret")):
            with db.tx() as con:   # same calendar low as last read and searched lately: skip
                same = [(lg, d) for lg, d in picked if cal.get(lg) and d in cal[lg]
                        and calendar_unchanged(con, w["id"], lg, d, cal[lg][d], cal_start)]
            if same:
                applog.info(f"{route}: {len(same)} date(s) skipped: calendar price unchanged since the "
                            "last search", user_id=user_id, watch_id=w["id"], source="run")
                picked = [x for x in picked if x not in same]
        return search_order(picked), to_search, used, False

    @staticmethod
    def _pause_after_block(user, e):
        """Stop pushing: pause this user's scheduled runs and tell them, once per block."""
        until = (dt.datetime.now() + dt.timedelta(hours=settings.block_pause_hours)).isoformat(timespec="minutes")
        with db.tx() as con:
            con.execute("UPDATE users SET blocked_until=?, blocked_reason=? WHERE id=?",
                        (until, f"{e.where}: {e.reason}", user["id"]))
        applog.error(f"Southwest blocked the {e.where} ({e.reason}). Run stopped; scheduled runs paused "
                     f"until {until.replace('T', ' ')}.", user_id=user["id"], source="run",
                     attachment=getattr(e, "screenshot", None))
        if user["email"]:
            mailer.send(user["email"], "Fare Watcher paused: Southwest blocked a request",
                        f"<p>While checking fares, Southwest showed a block or challenge page "
                        f"(<b>{html.escape(e.where)}</b>: {html.escape(e.reason)}).</p>"
                        f"<p>The run stopped straight away, and scheduled runs are paused until "
                        f"<b>{until.replace('T', ' ')}</b>. You can still press <i>Search now</i>; "
                        f"opening southwest.com yourself in a normal browser first can help.</p>"
                        f"<p>A screenshot is in the Log tab.</p>")

    @staticmethod
    def _finish_run(run_id, watch_id, note, searches, failures, matches):
        with db.tx() as con:
            con.execute("UPDATE runs SET finished=?, searches=COALESCE(NULLIF(?, 0), searches), failures=?,"
                        " matches=?, note=? WHERE id=?", (db.now(), searches, failures, matches, note, run_id))
            con.execute("UPDATE watches SET last_run_at=?, last_status=? WHERE id=?", (db.now(), note, watch_id))

    # ----- automatic Southwest sign-in -----
    MANUAL_ATTEMPTS_PER_DAY = 3
    MAX_CONSECUTIVE_FAILURES = 2

    def _attempts_today(self, user):
        return user["auto_login_attempts"] if user["auto_login_day"] == db.today() else 0

    def _sign_in(self, user, creds, searcher, manual=False):
        """Make sure the user's browser is signed in to Southwest before searching.
        Automatic attempts: at most 1 per day; 'Try sign-in now': up to 3 per day."""
        uid = user["id"]
        limit = self.MANUAL_ATTEMPTS_PER_DAY if manual else 1
        allowed = self._attempts_today(user) < limit
        def step(msg):  # sign-in steps from swa_login ("Clicking Log in", "Typing username", ...)
            self.phase(uid, "Sign-in: " + msg.split("] ", 1)[-1], source="signin")

        res = searcher.ensure_signed_in(creds["username"], creds["password"], allowed=allowed,
                                        code_provider=lambda: self._wait_for_code(user), log=step)
        attempted = res.status not in ("already_signed_in", "skipped")
        now = db.now()
        status, disable, notify = None, False, None
        failures = user["auto_login_failures"]
        if res.status == "success":
            status, failures = "Signed in automatically", 0
        elif res.status == "already_signed_in":
            status, failures = "Session still valid; no sign-in needed", 0
        elif res.status == "skipped":
            status = "Signed out; sign-in attempt limit reached for today"
        elif res.status == "bad_credentials":
            status, disable = "Turned off: Southwest rejected the username or password", True
            notify = ("Southwest rejected your saved username or password, so automatic sign-in is now off "
                      "(repeated attempts could lock your account). Update them under Settings and turn it back on.")
        elif res.status == "mfa_required":
            status = "Southwest asked for a verification code, and none was entered in time"
        else:
            failures += 1
            status = f"Sign-in failed: {res.status.replace('_', ' ')} ({res.detail})"
            if failures >= self.MAX_CONSECUTIVE_FAILURES:
                disable = True
                status = f"Turned off after {failures} failed attempts in a row: {res.status.replace('_', ' ')}"
                notify = ("Automatic sign-in to Southwest failed twice in a row, so it's now off. "
                          f"Last result: {res.status.replace('_', ' ')}. You can turn it back on under Settings, "
                          "or upload a session instead.")
        with db.tx() as con:
            day_attempts = self._attempts_today(user) + (1 if attempted else 0)
            con.execute(
                "UPDATE users SET auto_login_status=?, auto_login_at=?, auto_login_day=?, auto_login_attempts=?,"
                " auto_login_failures=?, auto_login=CASE WHEN ? THEN 0 ELSE auto_login END WHERE id=?",
                (status, now, db.today(), day_attempts, failures, int(disable), uid))
            if res.ok:
                con.execute("UPDATE users SET swa_status=?, swa_status_at=?, swa_state_updated=? WHERE id=?",
                            ("OK" if res.status == "already_signed_in" else "Signed in automatically", now,
                             now if res.status == "success" else user["swa_state_updated"], uid))
        if notify:
            mailer.send(user["email"], "SWA fare watcher: automatic sign-in turned off",
                        f"<p>{notify}</p><p><a href='{settings.base_url}/settings'>Open Settings</a></p>")
        applog.log(f"Sign-in result: {res.status.replace('_', ' ')} ({res.detail})",
                   "info" if res.ok or res.status == "skipped" else "warn", user_id=uid, source="signin",
                   attachment=getattr(res, "screenshot", None))
        return res

    def _wait_for_code(self, user):
        """Southwest wants a verification code: ask the user (email + banner in the app) and wait
        for them to enter it on the Verify page. Returns the code, or None if it doesn't arrive."""
        uid = user["id"]
        wait = settings.code_wait_seconds
        expires = (dt.datetime.now() + dt.timedelta(seconds=wait)).isoformat(timespec="seconds")
        with db.tx() as con:
            con.execute("UPDATE code_requests SET status='expired' WHERE user_id=? AND status='waiting'", (uid,))
            rid = con.execute("INSERT INTO code_requests(user_id, created_at, expires_at) VALUES (?,?,?)",
                              (uid, db.now(), expires)).lastrowid
        self.phase(uid, "Waiting for your verification code (enter it on the Verify page)", level="warn")
        mailer.send(user["email"], "Southwest verification code needed",
                    f"<p>Southwest sent you a verification code while the fare watcher was signing in. "
                    f"Enter it within {wait // 60} minutes:</p>"
                    f"<p><a href='{settings.base_url}/verify'>Enter the code</a></p>")
        end = time.monotonic() + wait
        while time.monotonic() < end:
            row = db.one("SELECT * FROM code_requests WHERE id=?", (rid,))
            if row["status"] == "submitted" and row["code"]:
                with db.tx() as con:
                    con.execute("UPDATE code_requests SET status='used', code=NULL WHERE id=?", (rid,))
                return row["code"]
            time.sleep(settings.code_poll_seconds)
        with db.tx() as con:
            con.execute("UPDATE code_requests SET status='expired' WHERE id=? AND status='waiting'", (rid,))
        return None


worker = Worker()
