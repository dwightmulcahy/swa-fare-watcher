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
        self.running_user = None
        self._threads = []

    # ----- queueing -----
    def enqueue(self, user_id, watch_ids=None):
        with self.lock:
            if user_id in self.pending:
                cur = self.pending[user_id]
                self.pending[user_id] = None if (cur is None or watch_ids is None) else cur | set(watch_ids)
                return
            self.pending[user_id] = None if watch_ids is None else set(watch_ids)
        self.q.put(user_id)

    def status(self, user_id):
        if self.running_user == user_id:
            return "running"
        return "queued" if user_id in self.pending else None

    def enqueue_all(self):
        rows = db.query("SELECT DISTINCT u.* FROM users u JOIN watches w ON w.user_id=u.id "
                        "WHERE w.active=1 AND u.active=1")
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
                self.running_user = uid
            try:
                self.run_user(uid, ids)
            except Exception:
                traceback.print_exc()
            finally:
                self.running_user = None

    def _schedule(self):
        print(f"[scheduler] daily at {settings.daily_at}", file=sys.stderr, flush=True)
        while True:
            time.sleep(seconds_until(settings.daily_at))
            n = self.enqueue_all()
            print(f"[scheduler] queued {n} user(s)", file=sys.stderr, flush=True)
            time.sleep(61)

    # ----- the actual run -----
    def run_user(self, user_id, watch_ids=None):
        user = db.one("SELECT * FROM users WHERE id=?", (user_id,))
        if not user or not db.user_can_run(user):
            return {"skipped": "user inactive"}
        watches = db.query("SELECT * FROM watches WHERE user_id=? AND active=1 ORDER BY id", (user_id,))
        if watch_ids is not None:
            watches = [w for w in watches if w["id"] in watch_ids]
        if not watches:
            return {"skipped": "no active watches"}

        state = decrypt_json(user["swa_state"])
        cap = user["max_searches_per_day"]
        hits, attempts, failures = [], 0, 0
        limited = False

        with self.searcher_factory(state, profile_key=user_id) as searcher:
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
        with db.tx() as con:
            if state is not None and new_state and not all_failed:
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
        if all_failed:
            mailer.send(user["email"], "SWA fare watcher: searches failed",
                        "<p>No fare data was returned for your watches. Your Southwest session may "
                        f"have expired. Upload a fresh one under <a href='{settings.base_url}/settings'>"
                        "Settings</a>.</p>")
        return {"attempts": attempts, "failures": failures, "hits": len(hits),
                "emailed": sent, "limited": limited}


worker = Worker()
