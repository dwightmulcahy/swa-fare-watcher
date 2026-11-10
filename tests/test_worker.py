import datetime as dt
import random

import pytest

from app import db
from app.security import decrypt_json, encrypt_json, hash_password
from app.worker import day_window, next_run_time, plan_day, search_order, search_pause, seconds_until
from tests.conftest import FakeSearcher, future


def make_user(**kw):
    cols = {"username": "u1", "email": "u1@example.com", "pw_hash": hash_password("x" * 10),
            "must_change_pw": 0, "created_at": db.now(), **kw}
    with db.tx() as con:
        return con.execute(f"INSERT INTO users({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                           tuple(cols.values())).lastrowid


def make_watch(uid, **kw):
    cols = {"user_id": uid, "name": "Trip", "origin": "AUS", "destination": "BWI",
            "dates": ",".join([future(10), future(11)]), "max_stops": 1, "max_price": 250,
            "max_points": 15000, "min_cpp": 1.3, "created_at": db.now(), **kw}
    with db.tx() as con:
        return con.execute(f"INSERT INTO watches({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                           tuple(cols.values())).lastrowid


def test_run_stores_fares_and_emails_new_matches(worker, sent):
    uid = make_user()
    wid = make_watch(uid)
    res = worker.run_user(uid)
    assert res == {"attempts": 4, "failures": 0, "hits": 2, "emailed": True, "limited": False, "cancelled": False}
    assert len(FakeSearcher.calls) == 4
    fares = db.query("SELECT * FROM fares WHERE watch_id=?", (wid,))
    assert len(fares) == 4 and sum(f["matched"] for f in fares) == 2
    assert sent[0][0] == "u1@example.com" and "1.54" in sent[0][2]
    assert db.searches_today(uid) == 4
    assert db.one("SELECT last_status FROM watches WHERE id=?", (wid,))["last_status"] == "2 matching flight(s)"

    # same prices again: nothing new to send
    sent.clear()
    assert worker.run_user(uid)["hits"] == 0
    assert sent == []


def test_daily_search_limit_stops_run(worker, sent):
    uid = make_user(max_searches_per_day=3)
    wid = make_watch(uid)
    res = worker.run_user(uid)
    assert res["attempts"] == 2 and res["limited"]
    assert db.one("SELECT note FROM runs WHERE watch_id=?", (wid,))["note"] == "Daily search limit reached"


def test_revoked_and_expired_users_are_skipped(worker):
    revoked = make_user(username="r", active=0)
    expired = make_user(username="e", expires_at=(dt.date.today() - dt.timedelta(days=1)).isoformat())
    for uid in (revoked, expired):
        make_watch(uid)
        assert worker.run_user(uid) == {"skipped": "user inactive"}
    assert FakeSearcher.calls == []


def test_session_is_refreshed_and_failures_reported(worker, sent):
    uid = make_user()
    make_watch(uid)
    with db.tx() as con:
        con.execute("UPDATE users SET swa_state=? WHERE id=?", (encrypt_json({"cookies": []}), uid))
    worker.run_user(uid)
    u = db.one("SELECT * FROM users WHERE id=?", (uid,))
    assert decrypt_json(u["swa_state"])["refreshed"] is True
    assert u["swa_status"] == "OK"

    FakeSearcher.fail = True
    sent.clear()
    worker.run_user(uid)
    u = db.one("SELECT * FROM users WHERE id=?", (uid,))
    assert u["swa_status"].startswith("Searches failed")
    assert sent[-1][1] == "SWA fare watcher: searches failed"


def test_enqueue_merges_and_only_queues_runnable_users(worker):
    a = make_user(username="a")
    make_watch(a)
    b = make_user(username="b", active=0)
    make_watch(b)
    assert worker.enqueue_all() == 1
    worker.enqueue(a, [5])
    assert worker.pending == {a: None} and worker.q.qsize() == 1
    assert worker.status(a) == "queued" and worker.status(b) is None


def test_seconds_until_wraps_to_tomorrow():
    now = dt.datetime(2026, 11, 6, 8, 0)
    assert seconds_until("07:20", now) == pytest.approx(23 * 3600 + 20 * 60)
    assert seconds_until("09:00", now) == 3600


def test_next_run_is_jittered_within_window_and_varies():
    now = dt.datetime(2026, 11, 8, 6, 0)
    times = [next_run_time("07:20", 45, now, rng=random.Random(i)) for i in range(200)]
    base = dt.datetime(2026, 11, 8, 7, 20)
    assert all(abs((t - base).total_seconds()) <= 45 * 60 for t in times)
    assert len({t.strftime("%H:%M") for t in times}) > 50      # different minute most days
    assert len({t.second for t in times}) > 30                 # and never on :00 seconds


def test_next_run_skips_today_once_run_or_past():
    now = dt.datetime(2026, 11, 8, 6, 0)
    t = next_run_time("07:20", 45, now, last_day=dt.date(2026, 11, 8), rng=random.Random(1))
    assert t.date() == dt.date(2026, 11, 9)
    late = dt.datetime(2026, 11, 8, 9, 0)  # window already over today
    assert next_run_time("07:20", 45, late, rng=random.Random(1)).date() == dt.date(2026, 11, 9)


def test_next_run_without_jitter_is_exact():
    now = dt.datetime(2026, 11, 8, 6, 0)
    assert next_run_time("07:20", 0, now) == dt.datetime(2026, 11, 8, 7, 20)


def test_next_run_started_mid_window_still_runs_today():
    now = dt.datetime(2026, 11, 8, 7, 30)  # window 06:35-08:05 partly gone
    for i in range(50):
        t = next_run_time("07:20", 45, now, rng=random.Random(i))
        assert dt.datetime(2026, 11, 8, 7, 31) <= t <= dt.datetime(2026, 11, 8, 8, 5)


def test_users_get_separate_random_starts_across_the_window():
    lo, hi = dt.datetime(2026, 11, 9, 6, 35), dt.datetime(2026, 11, 9, 8, 5)
    plan = plan_day([1, 2, 3, 4, 5], lo, hi, rng=random.Random(2))
    times = [t for t, _ in plan]
    assert times == sorted(times) and all(lo <= t <= hi for t in times)
    assert sorted(uid for _, uid in plan) == [1, 2, 3, 4, 5]
    gaps = [(b - a).total_seconds() for a, b in zip(times, times[1:])]
    assert sum(gaps) > 30 * 60  # spread over the window, not a back-to-back burst


def test_day_window_starts_now_if_already_open_and_skips_done_days():
    now = dt.datetime(2026, 11, 9, 7, 30)
    day, lo, hi = day_window("07:20", 45, now)
    assert day == now.date() and lo == now + dt.timedelta(seconds=60) and hi == dt.datetime(2026, 11, 9, 8, 5)
    day, lo, _ = day_window("07:20", 45, now, last_day=now.date())
    assert day == dt.date(2026, 11, 10) and lo == dt.datetime(2026, 11, 10, 6, 35)


def test_search_pauses_vary_naturally_with_occasional_breaks():
    rng = random.Random(5)
    d = sorted(search_pause(8, 20, rng) for _ in range(3000))
    median = d[1500]
    assert 11 < median < 17
    assert d[0] >= 4.8                          # never machine-gun fast
    assert sum(x > 45 for x in d) > 100         # some real breaks
    assert len({round(x, 1) for x in d}) > 500  # no fixed cadence


def test_search_order_varies():
    rng = random.Random(1)
    dates = ["2026-12-01", "2026-12-02", "2026-12-03", "2026-12-04"]
    orders = {tuple(search_order(dates, rng)) for _ in range(200)}
    assert tuple(dates) in orders and tuple(reversed(dates)) in orders and len(orders) > 5


def test_cash_and_points_order_varies(worker, sent):
    uid = make_user()
    make_watch(uid, dates=",".join(future(d) for d in range(10, 20)))
    worker.run_user(uid)
    firsts = [c[3] for c in FakeSearcher.calls[::2]]  # first fare type searched for each date
    assert set(firsts) == {"USD", "POINTS"}



def test_no_flights_day_skips_the_other_currency(worker, sent):
    from tests.conftest import FakeSearcher

    class NoFlights(FakeSearcher):
        def search(self, origin, dest, date, adults, fare_type):
            FakeSearcher.calls.append((origin, dest, date, fare_type))
            return {}

    FakeSearcher.calls = []
    worker.searcher_factory = NoFlights
    uid = make_user()
    wid = make_watch(uid)                    # 2 dates
    res = worker.run_user(uid)
    assert len(FakeSearcher.calls) == 2      # one currency per date, not two
    assert res["attempts"] == 2 and res["failures"] == 0 and res["hits"] == 0
    assert "failed" not in (db.one("SELECT last_status FROM watches WHERE id=?", (wid,))["last_status"] or "")


def test_block_page_stops_run_pauses_schedule_and_emails(worker, sent):
    import pytest

    from app import swa
    from tests.conftest import FakeSearcher

    class BlockedAfterOne(FakeSearcher):
        n = 0

        def search(self, origin, dest, date, adults, fare_type):
            BlockedAfterOne.n += 1
            if BlockedAfterOne.n > 1:
                raise swa.Blocked("flight search", 'page says "Access Denied"')
            return super().search(origin, dest, date, adults, fare_type)

    worker.searcher_factory = BlockedAfterOne
    uid = make_user(email="me@example.com")
    wid = make_watch(uid)
    with pytest.raises(swa.Blocked):
        worker.run_user(uid)
    assert BlockedAfterOne.n == 2                                   # nothing after the block page
    u = db.one("SELECT * FROM users WHERE id=?", (uid,))
    assert u["blocked_until"] > db.now() and "Access Denied" in u["blocked_reason"]
    assert uid not in worker.eligible_users()                       # scheduler skips this user
    assert "blocked" in db.one("SELECT last_status FROM watches WHERE id=?", (wid,))["last_status"]
    assert any("paused" in s[1] for s in sent)                      # user told once


def test_currency_on_screen_is_searched_first(worker, sent):
    from tests.conftest import FakeSearcher

    class OnScreen(FakeSearcher):
        def shown_currency(self, origin, dest, adults):
            return "POINTS"                    # e.g. the last results shown were in points

    FakeSearcher.calls = []
    worker.searcher_factory = OnScreen
    uid = make_user()
    make_watch(uid)                            # 2 dates
    worker.run_user(uid)
    firsts = [c[3] for c in FakeSearcher.calls[::2]]
    assert firsts == ["POINTS", "POINTS"]      # each date starts in the currency on screen


def test_cancel_stops_after_current_search_and_keeps_results(worker, sent):
    uid = make_user()
    wid = make_watch(uid)
    make_watch(uid, name="Second")
    worker.running_user = uid
    calls = []

    def sleep(_):   # cancel during the pause after the first search
        calls.append(1)
        worker.cancel(uid)
    worker.sleep = sleep
    out = worker.run_user(uid)
    assert out["cancelled"] and out["attempts"] == 1
    assert len(FakeSearcher.calls) == 1   # the other currency, date and watch were skipped
    assert db.query("SELECT COUNT(*) n FROM fares")[0]["n"] > 0   # the first search's fares are saved
    notes = [r["note"] for r in db.query("SELECT note FROM runs")]
    assert len(notes) == 1 and notes[0].startswith("Cancelled after 1 search")
    assert db.one("SELECT last_status FROM watches WHERE id=?", (wid,)) is not None


def test_cancel_queued_run_is_dropped(worker):
    uid = make_user()
    make_watch(uid)
    worker.enqueue(uid)
    assert worker.cancel(uid) == "queued"
    assert worker.cancel(make_user(username="u2", email="b@x")) is None
