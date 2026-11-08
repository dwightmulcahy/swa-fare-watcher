"""Background worker: runs one user's watches at a time in a single browser, plus a daily scheduler."""
import datetime as dt
import queue
import random
import sys
import threading
import time
import traceback

from . import db, mailer, swa
from .config import settings
from .security import decrypt_json, encrypt_json


def seconds_until(hhmm, now=None):
    now = now or dt.datetime.now()
    h, m = map(int, hhmm.split(":"))
    nxt = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if nxt <= now:
        nxt += dt.timedelta(days=1)
    return (nxt - now).total_seconds()


def next_run_time(hhmm, jitter_min, now=None, last_day=None, rng=random):
    """When the next daily run should start: hh:mm plus a fresh random offset within
    ±jitter_min, so runs never start at the same minute (or second) two days in a row.
    Skips any day already run (last_day) and any time already past."""
    now = now or dt.datetime.now()
    h, m = map(int, hhmm.split(":"))
    for add in range(0, 3):
        day = now.date() + dt.timedelta(days=add)
        if last_day is not None and day <= last_day:
            continue
        base = dt.datetime.combine(day, dt.time(h, m))
        lo = base - dt.timedelta(minutes=jitter_min)
        hi = base + dt.timedelta(minutes=jitter_min)
        if hi <= now:
            continue  # today's window is over
        lo = max(lo, now + dt.timedelta(seconds=60))  # started mid-window: pick from what's left
        if lo >= hi:
            when = hi
        else:
            when = lo + dt.timedelta(seconds=rng.uniform(0, (hi - lo).total_seconds()))
        return when.replace(microsecond=0)
    raise RuntimeError("no next run time")  # unreachable with sane input


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
        self.next_run = None
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

    def status(self, user_id):
        if self.running_user == user_id:
            return "running"
        return "queued" if user_id in self.pending else None

    def enqueue_all(self):
        rows = db.query("SELECT DISTINCT u.* FROM users u JOIN watches w ON w.user_id=u.id "
                        "WHERE w.active=1 AND u.active=1")
        rows = list(rows)
        random.shuffle(rows)  # a different order every day
        for u in rows:
            if db.user_can_run(u):
                self.enqueue(u["id"])
        return len(rows)

    def start(self, scheduler=True):
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
            except Exception:
                traceback.print_exc()
            finally:
                self.running_user = None

    def _schedule(self):
        last_day = None
        while True:
            when = next_run_time(settings.daily_at, settings.daily_jitter_min, last_day=last_day)
            print(f"[scheduler] next run {when:%Y-%m-%d %H:%M:%S} "
                  f"({settings.daily_at} ±{settings.daily_jitter_min} min)", file=sys.stderr, flush=True)
            self.next_run = when
            while (wait := (when - dt.datetime.now()).total_seconds()) > 0:
                time.sleep(min(wait, 300))  # re-check: survives clock changes and sleep/suspend
            last_day = when.date()
            n = self.enqueue_all()
            print(f"[scheduler] queued {n} user(s)", file=sys.stderr, flush=True)

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

        with self.searcher_factory(state, profile_key=user_id) as searcher:
            if creds and hasattr(searcher, "ensure_signed_in"):
                signin = self._sign_in(user, creds, searcher, manual=signin_only)
            for w in watches:
                run_id = None
                with db.tx() as con:
                    run_id = con.execute("INSERT INTO runs(watch_id, started) VALUES (?,?)",
                                         (w["id"], db.now())).lastrowid
                w_s = w_f = w_m = 0
                note = None
                for date in swa.watch_dates(w):
                    if cap is not None and db.searches_today(user_id) + 2 > cap:
                        note, limited = "Daily search limit reached", True
                        break
                    res = {}
                    for ft in ("USD", "POINTS"):
                        res[ft] = searcher.search(w["origin"], w["destination"], date, w["adults"], ft)
                        with db.tx() as con:
                            db.add_searches(con, user_id, 1)
                        attempts += 1
                        w_s += 1
                        if res[ft] is None:
                            failures += 1
                            w_f += 1
                        self.sleep(random.uniform(*settings.search_delay))
                    ts = db.now()
                    with db.tx() as con:
                        for r in swa.combine(res["USD"], res["POINTS"]):
                            matched = swa.passes(r, w)
                            con.execute(
                                "INSERT INTO fares(watch_id, ts, date, flight_key, flights, depart, arrive,"
                                " stops, duration, cash, points, points_taxes, cpp, matched)"
                                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                (w["id"], ts, date, r["key"], r["flights"], r["depart"], r["arrive"],
                                 r["stops"], r["duration"], r["cash"], r["points"], r["points_taxes"],
                                 r["cpp"], int(matched)))
                            if not matched:
                                continue
                            w_m += 1
                            key = f"{date}|{r['key']}"
                            if _is_new_low(con, w["id"], key, r):
                                hits.append({
                                    "watch_id": w["id"], "key": key, "row": r, "date": date,
                                    "watch": f'{w["name"]} ({w["origin"]}→{w["destination"]})',
                                    "use": swa.recommend(r, w),
                                    "url": swa.booking_url(w["origin"], w["destination"], date, w["adults"]),
                                })
                if note is None:
                    note = ("All searches failed" if w_s and w_f == w_s
                            else f"{w_m} matching flight(s)" if w_s else "No upcoming dates")
                with db.tx() as con:
                    con.execute("UPDATE runs SET finished=?, searches=?, failures=?, matches=?, note=? WHERE id=?",
                                (db.now(), w_s, w_f, w_m, note, run_id))
                    con.execute("UPDATE watches SET last_run_at=?, last_status=? WHERE id=?",
                                (db.now(), note, w["id"]))
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

        sent = False
        if hits:
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
        res = searcher.ensure_signed_in(creds["username"], creds["password"], allowed=allowed,
                                        code_provider=lambda: self._wait_for_code(user))
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
        print(f"[signin] user {uid}: {res.status} ({res.detail})", file=sys.stderr, flush=True)
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
