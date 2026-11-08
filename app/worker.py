"""Background worker: runs one user's watches at a time in a single browser, plus a daily scheduler."""
import datetime as dt
import math
import queue
import random
import threading
import time

from . import applog, db, mailer, swa
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


def _is_new_low(con, watch_id, key, r):
    row = con.execute("SELECT cash, points FROM alerted WHERE watch_id=? AND key=?",
                      (watch_id, key)).fetchone()
    if row is None:
        return True
    lc, lp = row["cash"], row["points"]
    return (r["cash"] is not None and (lc is None or r["cash"] < lc)) or (
        r["points"] is not None and (lp is None or r["points"] < lp))


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
        self._threads = []

    # ----- queueing -----
    def enqueue(self, user_id, watch_ids=None):
        with self.lock:
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

    # ----- live progress (shown in the UI while a run is going) -----
    def phase(self, user_id, text, watch_id=None, done=None, total=None, log=True, level="info", source="run"):
        p = self.progress.setdefault(user_id, {"started": db.now()})
        p.update(phase=text, at=db.now())
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
        return {"state": state, **({k: p.get(k) for k in ("phase", "done", "total", "watch_id", "started", "at")}
                                   if state == "running" else {})}

    def status(self, user_id):
        if self.running_user == user_id:
            return "running"
        return "queued" if user_id in self.pending else None

    def eligible_users(self):
        rows = db.query("SELECT DISTINCT u.* FROM users u JOIN watches w ON w.user_id=u.id "
                        "WHERE w.active=1 AND u.active=1")
        return [u["id"] for u in rows if db.user_can_run(u)]

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
                self.running_user = uid
            try:
                self.run_user(uid, ids, signin_only=only)
            except Exception as e:
                if not getattr(e, "logged", False):
                    applog.error(f"Run stopped by an error: {e.__class__.__name__}: {e}", user_id=uid,
                                 source="run", exc=True)
            finally:
                self.running_user = None
                self.progress.pop(uid, None)

    def _sleep_until(self, when):
        while (wait := (when - dt.datetime.now()).total_seconds()) > 0:
            time.sleep(min(wait, 300))  # re-check: survives clock changes and sleep/suspend

    def _schedule(self):
        last_day = None
        while True:
            day, lo, hi = day_window(settings.daily_at, settings.daily_jitter_min, last_day=last_day)
            self.next_window = (lo, hi)
            applog.info(f"Next daily run window {lo:%a %Y-%m-%d %H:%M}-{hi:%H:%M}", source="scheduler")
            self._sleep_until(lo)
            plan = plan_day(self.eligible_users(), dt.datetime.now(), hi)
            self.planned = {uid: t for t, uid in plan}
            applog.info(f"Today's plan, {len(plan)} user(s): "
                        + (", ".join(f"user {uid} at {t:%H:%M:%S}" for t, uid in plan) or "nobody to run"),
                        source="scheduler")
            for t, uid in plan:
                self._sleep_until(t)
                self.enqueue(uid)
                self.planned.pop(uid, None)
            last_day = day

    def next_run_for(self, user_id):
        """This user's planned start today, or the next window (start, end)."""
        if user_id in self.planned:
            return self.planned[user_id], None
        return None, self.next_window

    # ----- the actual run -----
    def run_user(self, user_id, watch_ids=None, signin_only=False):
        user = db.one("SELECT * FROM users WHERE id=?", (user_id,))
        if not user or not db.user_can_run(user):
            return {"skipped": "user inactive"}
        creds = decrypt_json(user["swa_creds"]) if (settings.auto_login and user["auto_login"]) else None
        watches = [] if signin_only else db.query(
            "SELECT * FROM watches WHERE user_id=? AND active=1 ORDER BY id", (user_id,))
        if watch_ids is not None:
            watches = [w for w in watches if w["id"] in watch_ids]
        if not watches and not (signin_only and creds):
            return {"skipped": "no active watches" if not signin_only else "automatic sign-in not set up"}

        state = decrypt_json(user["swa_state"])
        cap = user["max_searches_per_day"]
        hits, attempts, failures = [], 0, 0
        limited = False
        signin = None

        watches = random.sample(watches, len(watches))  # different watch order each day
        plan = [(w, search_order(swa.search_legs(w))) for w in watches]  # [(leg, date)], legs mixed
        total = sum(2 * len(d) for _, d in plan)
        done = 0
        self.phase(user_id, "Sign-in check only" if signin_only else
                   f"Starting a run: {len(plan)} watch(es), up to {total} searches", done=0, total=total)
        open_runs = {}  # watch_id -> run_id not yet finished (closed even if something breaks)
        searcher = None
        try:
            self.phase(user_id, "Starting the browser")
            with self.searcher_factory(state, profile_key=user_id) as searcher:
                try:
                    if creds and hasattr(searcher, "ensure_signed_in"):
                        self.phase(user_id, "Opening southwest.com and checking you're signed in")
                        signin = self._sign_in(user, creds, searcher, manual=signin_only)
                    elif plan:
                        self.phase(user_id, "Opening southwest.com")
                    for w, dates in plan:
                        route = f'{w["name"]} ({w["origin"]}→{w["destination"]})'
                        with db.tx() as con:
                            open_runs[w["id"]] = con.execute("INSERT INTO runs(watch_id, started) VALUES (?,?)",
                                                             (w["id"], db.now())).lastrowid
                        with db.tx() as con:
                            con.execute("UPDATE watches SET last_status=? WHERE id=?", ("Running…", w["id"]))
                        rt = swa.is_roundtrip(w)
                        n_out = sum(1 for leg, _ in dates if leg == "out")
                        self.phase(user_id, f"{route}: {n_out} outbound" + (f" and {len(dates) - n_out} return"
                                   if rt else "") + " date(s) to search", watch_id=w["id"])
                        w_s = w_f = w_m = 0
                        note = None
                        if settings.calendar and hasattr(searcher, "calendar") and dates:
                            before = 2 * len(dates)
                            dates, used, hit_cap = self._calendar_phase(user_id, w, route, dates, searcher, cap)
                            w_s += used
                            total += used + 2 * len(dates) - before
                            done += used
                            self.phase(user_id, f"{route}: calendar narrowed it to {len(dates)} date(s) to search",
                                       watch_id=w["id"], done=done, total=total)
                            if hit_cap:
                                note, limited, dates = "Daily search limit reached", True, []
                            elif not dates:
                                note = "No date on the calendar under your target"
                        legs = {"out": {}, "ret": {}}  # this run's flights per leg and date
                        for leg, date in dates:
                            if cap is not None and db.searches_today(user_id) + 2 > cap:
                                note, limited = "Daily search limit reached", True
                                self.phase(user_id, f"{route}: daily search limit reached, stopping",
                                           watch_id=w["id"], level="warn")
                                break
                            frm, to = (w["origin"], w["destination"]) if leg == "out" else (w["destination"], w["origin"])
                            res = {}
                            for ft in random.sample(("USD", "POINTS"), 2):  # cash or points first
                                done += 1
                                self.phase(user_id, f"{route}: searching {frm}→{to} {date} "
                                           f"({'cash' if ft == 'USD' else 'points'})",
                                           watch_id=w["id"], done=done, total=total)
                                res[ft] = searcher.search(frm, to, date, w["adults"], ft)
                                with db.tx() as con:
                                    db.add_searches(con, user_id, 1)
                                attempts += 1
                                w_s += 1
                                if res[ft] is None:
                                    failures += 1
                                    w_f += 1
                                    self.phase(user_id, f"{route}: {frm}→{to} {date} {ft.lower()} search returned no fares",
                                               watch_id=w["id"], level="warn")
                                pause = search_pause(*settings.search_delay)
                                self.phase(user_id, f"Pausing {pause:.0f} s before the next search",
                                           watch_id=w["id"], log=False)
                                self.sleep(pause)
                            ts = db.now()
                            n_flights = n_match = 0
                            rows = swa.combine(res["USD"], res["POINTS"])
                            legs[leg][date] = rows
                            with db.tx() as con:
                                for r in rows:
                                    n_flights += 1
                                    # one-way: the flight must match on its own; round trip: totals decide below
                                    matched = swa.passes(r, w) if not rt else swa.passes_leg(r, w, leg)
                                    con.execute(
                                        "INSERT INTO fares(watch_id, ts, date, leg, flight_key, flights, depart, arrive,"
                                        " stops, duration, cash, points, points_taxes, cpp, matched)"
                                        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                        (w["id"], ts, date, leg, r["key"], r["flights"], r["depart"], r["arrive"],
                                         r["stops"], r["duration"], r["cash"], r["points"], r["points_taxes"],
                                         r["cpp"], int(matched)))
                                    if not matched or rt:
                                        continue
                                    w_m += 1
                                    n_match += 1
                                    key = f"{date}|{r['key']}"
                                    if _is_new_low(con, w["id"], key, r):
                                        hits.append({
                                            "watch_id": w["id"], "key": key, "row": r, "date": date,
                                            "watch": route, "use": swa.recommend(r, w),
                                            "url": swa.booking_url(w["origin"], w["destination"], date, w["adults"]),
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
                        if rt:
                            trips = swa.roundtrips(w, legs)
                            with db.tx() as con:
                                for t in trips:
                                    if not t["matched"]:
                                        continue
                                    w_m += 1
                                    key = f"rt|{t['out_date']}|{t['ret_date']}"
                                    if _is_new_low(con, w["id"], key, {"cash": t["cash"], "points": t["points"]}):
                                        hits.append({
                                            "watch_id": w["id"], "key": key, "trip": t, "date": t["out_date"],
                                            "row": {"cash": t["cash"], "points": t["points"]},
                                            "watch": route, "use": t["use"],
                                            "url": swa.booking_url(w["origin"], w["destination"], t["out_date"],
                                                                   w["adults"], t["ret_date"]),
                                        })
                            applog.info(f"{route}: {len(trips)} round trip(s) priced, {w_m} matching",
                                        user_id=user_id, watch_id=w["id"], source="run")
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
            applog.error(f"Run stopped by an error: {e.__class__.__name__}: {e}", user_id=user_id, source="run",
                         exc=True, attachment=getattr(e, "screenshot", None))
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

        applog.info(f"Run finished: {attempts} search(es), {failures} failed, {len(hits)} new or lower fare(s)"
                    + (" (daily limit reached)" if limited else ""), user_id=user_id, source="run")
        sent = False
        if hits:
            self.phase(user_id, f"Emailing {len(hits)} fare alert(s)", log=False)
            sent = mailer.send(user["email"], f"SWA fare alert: {len(hits)} match(es)",
                               mailer.render_hits(hits))
            if sent:
                with db.tx() as con:
                    for h in hits:
                        con.execute("INSERT OR REPLACE INTO alerted VALUES (?,?,?,?)",
                                    (h["watch_id"], h["key"], h["row"]["cash"], h["row"]["points"]))
        if all_failed and not (creds and signin is not None and signin.status in ("success", "already_signed_in")):
            mailer.send(user["email"], "SWA fare watcher: searches failed",
                        "<p>No fare data was returned for your watches. Your Southwest session may "
                        f"have expired. Upload a fresh one under <a href='{settings.base_url}/settings'>"
                        "Settings</a>.</p>")
        out = {"attempts": attempts, "failures": failures, "hits": len(hits),
               "emailed": sent, "limited": limited}
        if signin is not None:
            out["signin"] = signin.status
        return out

    def _calendar_phase(self, user_id, w, route, dates, searcher, cap):
        """Read the low-fare calendar (cash and points, each month and direction) and keep the
        dates worth a full search. Returns (dates, calendar loads used, hit the daily cap)."""
        cal, used = {}, 0
        for leg in ("out", "ret"):
            leg_dates = [d for lg, d in dates if lg == leg]
            if not leg_dates:
                continue
            frm, to = (w["origin"], w["destination"]) if leg == "out" else (w["destination"], w["origin"])
            lows, ok = {}, True
            months = list(swa.calendar_months(leg_dates).items())
            for month, first in random.sample(months, len(months)):
                for ft in random.sample(("USD", "POINTS"), 2):
                    if cap is not None and db.searches_today(user_id) + 1 > cap:
                        self.phase(user_id, f"{route}: daily search limit reached, stopping",
                                   watch_id=w["id"], level="warn")
                        return [], used, True
                    self.phase(user_id, f"{route}: reading the {month} {'cash' if ft == 'USD' else 'points'} "
                               f"calendar {frm}→{to}", watch_id=w["id"])
                    days = searcher.calendar(frm, to, first, w["adults"], ft)
                    with db.tx() as con:
                        db.add_searches(con, user_id, 1)
                    used += 1
                    if days is None:
                        ok = False
                    else:
                        field = "cash" if ft == "USD" else "points"
                        for d, v in days.items():
                            lows.setdefault(d, {"cash": None, "points": None})[field] = v
                    self.sleep(search_pause(*settings.search_delay))
            cal[leg] = lows if ok else None
            if ok:
                ts = db.now()
                with db.tx() as con:
                    con.executemany(
                        "INSERT INTO calendar_fares(watch_id, ts, leg, date, cash, points) VALUES (?,?,?,?,?,?)",
                        [(w["id"], ts, leg, d, v["cash"], v["points"]) for d, v in sorted(lows.items())])
                cheap = min((v["cash"] for d, v in lows.items() if d in leg_dates and v["cash"] is not None),
                            default=None)
                applog.info(f"{route}: {frm}→{to} calendar read for {len(leg_dates)} date(s)"
                            + (f", lowest ${cheap:,.0f} per person" if cheap is not None else ""),
                            user_id=user_id, watch_id=w["id"], source="run")
            else:
                applog.warn(f"{route}: {frm}→{to} calendar unreadable; searching every date",
                            user_id=user_id, watch_id=w["id"], source="run")
        picked = swa.calendar_pick(w, cal, dates, settings.calendar_detail_dates)
        return search_order(picked), used, False

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
