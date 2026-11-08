import datetime as dt
import random

import pytest

from app import db
from app.security import decrypt_json, encrypt_json, hash_password
from app.worker import next_run_time, seconds_until
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
    assert res == {"attempts": 4, "failures": 0, "hits": 2, "emailed": True, "limited": False}
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
