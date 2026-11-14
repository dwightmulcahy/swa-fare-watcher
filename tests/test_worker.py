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


def test_stale_runs_email_admins_once(worker, sent, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "scheduler", True)
    admin = make_user(username="adm", email="adm@example.com", is_admin=1)
    make_watch(admin)
    worker.started_at = dt.datetime(2027, 1, 1, 8, 0)
    assert worker.check_stale(now=dt.datetime(2027, 1, 2, 9, 0)) is False      # 25 h
    assert worker.check_stale(now=dt.datetime(2027, 1, 2, 11, 0)) is True      # 27 h
    assert worker.check_stale(now=dt.datetime(2027, 1, 2, 12, 0)) is True
    assert [s[0] for s in sent] == ["adm@example.com"] and "no searches" in sent[0][1]
    with db.tx() as con:   # a run finishes: the alert resets
        wid = con.execute("SELECT id FROM watches").fetchone()[0]
        con.execute("INSERT INTO runs(watch_id, started, finished) VALUES (?,?,?)",
                    (wid, "2027-01-02T12:30:00", "2027-01-02T12:40:00"))
    assert worker.check_stale(now=dt.datetime(2027, 1, 2, 13, 0)) is False
    assert db.get_state("stale_alerted") == ""


def test_worker_alive_reflects_threads(worker):
    import threading
    t = threading.Thread(target=lambda: None)
    t.start()
    t.join()
    worker._threads = [t]
    assert worker.alive() is False


def test_format_change_warns_admins_once_a_day(sent):
    from app.worker import Worker
    make_user(username="adm", email="adm@example.com", is_admin=1)
    assert Worker.check_format(10, 1, 1, day="2027-01-01") is False     # a couple of odd days is normal
    assert Worker.check_format(2, 2, 0, day="2027-01-01") is False      # too few searches to tell
    assert Worker.check_format(6, 2, 2, day="2027-01-01") is True
    assert Worker.check_format(6, 4, 0, day="2027-01-01") is True       # same day: no second email
    assert len(sent) == 1 and "changed its fare data" in sent[0][1]
    Worker.check_format(4, 0, 4, day="2027-01-02")
    assert len(sent) == 2


def test_priceless_detects_flights_without_fares():
    from app import swa
    assert swa.priceless({"a": {"fare": None}, "b": {"fare": None}})
    assert not swa.priceless({"a": {"fare": None}, "b": {"fare": (100.0, 5.6)}})
    assert not swa.priceless({}) and not swa.priceless(None)


def test_users_keep_their_own_time_and_start_apart():
    from app.worker import plan_users, user_offset
    assert user_offset(1, 120) == user_offset(1, 120) and user_offset(1, 120) != user_offset(2, 120)
    assert all(-120 <= user_offset(u, 120) <= 120 for u in range(100)) and user_offset(5, 0) == 0
    day, now = dt.date(2027, 1, 5), dt.datetime(2027, 1, 5, 4, 0)
    plan = plan_users(list(range(1, 9)), day, now, "07:20", 120, 30, rng=random.Random(3))
    times = [t for t, _ in plan]
    assert times == sorted(times) and all(b - a >= dt.timedelta(minutes=20) for a, b in zip(times, times[1:]))
    for u in (1, 2, 3):   # each user near their usual time, on different days
        usual = dt.datetime(2027, 1, 5, 7, 20) + dt.timedelta(minutes=user_offset(u, 120))
        t = dict((uid, t) for t, uid in plan_users([u], day, now, "07:20", 120, 30, rng=random.Random(9)))[u]
        assert abs(t - usual) <= dt.timedelta(minutes=30, seconds=1)
    late = dt.datetime(2027, 1, 5, 12, 0)
    assert all(t > late for t, _ in plan_users([1, 2], day, late, "07:20", 120, 30))


def test_skip_day_never_twice_running_or_with_a_booking():
    import json
    from app.worker import skip_today

    class Always:
        def random(self): return 0.0
    uid = make_user()
    d1, d2, d3 = dt.date(2027, 1, 5), dt.date(2027, 1, 6), dt.date(2027, 1, 7)
    assert skip_today(uid, d1, 0.1, rng=Always()) is True
    assert skip_today(uid, d2, 0.1, rng=Always()) is False        # skipped yesterday
    assert skip_today(uid, d3, 0.1, rng=Always()) is True
    assert skip_today(uid, d3, 0.0, rng=Always()) is False
    other = make_user(username="b2", email="b2@x")
    make_watch(other, booked=json.dumps([{"leg": "out", "date": "2027-02-01", "cash": 200}]))
    assert skip_today(other, d1, 0.5, rng=Always()) is False      # a booked flight to watch


def test_run_is_split_into_visits_with_breaks(worker, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "visit_searches", (3, 3))
    monkeypatch.setattr(settings, "visit_break", (20, 20))
    opened = []
    factory = worker.searcher_factory

    def counting(state, profile_key=None):
        opened.append(state)
        return factory(state, profile_key=profile_key)
    worker.searcher_factory = counting
    slept = []
    worker.sleep = slept.append
    uid = make_user()
    make_watch(uid, dates=",".join(future(i) for i in range(10, 16)))   # 6 dates x 2 currencies
    out = worker.run_user(uid)
    assert out["attempts"] == 12
    assert len(opened) == 3                       # 3 + 3 searches, then the last 6 in visit 3 (only
                                                  # a few left after its 3rd: they're just finished)
    assert slept.count(1200) == 2                 # a 20-minute break between visits


def test_visits_off_keeps_one_browser(worker, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "visit_searches", (0, 0))
    opened = []
    factory = worker.searcher_factory
    worker.searcher_factory = lambda st, profile_key=None: opened.append(1) or factory(st, profile_key=profile_key)
    uid = make_user()
    make_watch(uid, dates=",".join(future(i) for i in range(10, 15)))
    worker.run_user(uid)
    assert len(opened) == 1


def test_gpu_warning_email_once_a_day(sent, monkeypatch):
    from app import swa
    from app.config import settings
    from app.worker import Worker
    make_user(username="adm", email="adm@example.com", is_admin=1)

    class B:
        def __init__(self, gl): self.gl = gl
        def renderer(self): return self.gl
    monkeypatch.setattr(settings, "gpu", False)
    sw = "ANGLE (Google, Vulkan 1.3.0 (SwiftShader Device (Subzero)), SwiftShader driver)"
    assert Worker.check_gpu(B(sw), day="2027-01-01") is False
    assert len(sent) == 1 and "GPU rendering is off" in sent[0][1] and "SWA_GPU isn&#x27;t set" in sent[0][2]
    assert Worker.check_gpu(B(sw), day="2027-01-01") is None              # once a day
    monkeypatch.setattr(settings, "gpu", True)
    monkeypatch.setattr(settings, "gpu_device", "/nonexistent/renderD128")
    Worker.check_gpu(B(sw), day="2027-01-02")
    assert "exist in the container" in sent[1][2]
    assert Worker.check_gpu(B("ANGLE (Intel, Vulkan 1.3 (Intel(R) UHD Graphics 600))"), day="2027-01-03") is True
    assert len(sent) == 2
    assert swa.gpu_problem().startswith("SWA_GPU is on but")


def test_far_dates_are_searched_less_often():
    from app.worker import due_legs, record_searched, search_every
    assert [search_every(d) for d in (3, 14, 30, 90, 200)] == [1, 1, 2, 3, 5]
    uid = make_user()
    today = dt.date.today()
    dates = [(today + dt.timedelta(days=n)).isoformat() for n in (5, 6, 7, 8, 30, 31, 100, 101, 200)]
    wid = make_watch(uid, dates=",".join(dates))
    w = db.one("SELECT * FROM watches WHERE id=?", (wid,))
    legs = [("out", d) for d in dates]
    assert due_legs(w, legs, today) == legs                       # never searched: all due
    with db.tx() as con:
        for leg in legs:
            record_searched(con, wid, *leg, [{"cash": 100.0, "points": 5000}])
    assert due_legs(w, legs, today) == []                         # all searched today
    tomorrow = today + dt.timedelta(days=1)
    assert [d for _, d in due_legs(w, legs, tomorrow)] == dates[:4]   # only the near ones daily
    in3 = today + dt.timedelta(days=3)
    assert len(due_legs(w, legs, in3)) == 8                       # 30/31 (every 2) and 100/101 (every 3)
    assert due_legs(w, legs[:8], tomorrow) == legs[:8]            # a small watch: everything, every day
    assert due_legs(w, legs[:8], today) == []                     # but not twice in one day


def test_budget_keeps_nearest_dates(monkeypatch):
    from app.config import settings
    from app.worker import fit_budget, page_budget
    monkeypatch.setattr(settings, "page_budget", (20, 35))
    w1 = {"id": 1, "origin": "DAL", "destination": "HOU", "trip_type": "oneway"}
    w2 = {"id": 2, "origin": "DAL,DFW", "destination": "HOU", "trip_type": "oneway"}
    plan = [(w1, [("out", "2027-03-01"), ("out", "2027-01-05")]), (w2, [("out", "2027-01-10")])]
    kept = fit_budget(plan, 6)          # w1 costs 2 per date, w2 4 (two airports)
    assert kept == [(w1, [("out", "2027-01-05")]), (w2, [("out", "2027-01-10")])]
    assert fit_budget(plan, None) == plan
    b = page_budget(7, "2027-01-05")
    assert b == page_budget(7, "2027-01-05") and 20 <= b <= 35


def test_record_searched_tracks_price_changes():
    from app.worker import record_searched
    uid = make_user()
    wid = make_watch(uid)
    with db.tx() as con:
        record_searched(con, wid, "out", "2027-01-05", [{"cash": 100.0, "points": None}])
    first = db.one("SELECT * FROM searched")
    assert first["changed_at"] == first["ts"] and first["cash"] == 100.0
    with db.tx() as con:
        con.execute("UPDATE searched SET ts='2027-01-01T07:00:00', changed_at='2027-01-01T07:00:00'")
        record_searched(con, wid, "out", "2027-01-05", [{"cash": 100.0, "points": None}])
    again = db.one("SELECT * FROM searched")
    assert again["changed_at"] == "2027-01-01T07:00:00" and again["ts"] != "2027-01-01T07:00:00"
    with db.tx() as con:
        record_searched(con, wid, "out", "2027-01-05", [{"cash": 89.0, "points": None}])
    assert db.one("SELECT * FROM searched")["changed_at"] != "2027-01-01T07:00:00"


def test_calendar_unchanged_skips_recently_searched_dates():
    from app.worker import calendar_unchanged, record_searched
    uid = make_user()
    wid = make_watch(uid)
    with db.tx() as con:
        con.execute("INSERT INTO calendar_fares(watch_id, ts, leg, origin, dest, date, cash, points) "
                    "VALUES (?,?,?,?,?,?,?,?)", (wid, "2026-01-01T07:00:00", "out", "DAL", "HOU", "2027-01-05", 99.0, 6000))
        assert not calendar_unchanged(con, wid, "out", "2027-01-05", {"cash": 99.0, "points": 6000}, "2099-01-01")
        record_searched(con, wid, "out", "2027-01-05", [])
        assert calendar_unchanged(con, wid, "out", "2027-01-05", {"cash": 99.0, "points": 6000}, "2099-01-01")
        assert not calendar_unchanged(con, wid, "out", "2027-01-05", {"cash": 89.0, "points": 6000}, "2099-01-01")


def test_scheduled_run_rotates_but_search_now_does_everything(worker, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "rotate", True)
    if True:
        uid = make_user()
        today = dt.date.today()
        dates = [(today + dt.timedelta(days=n)).isoformat() for n in (3, 4, 5, 6, 60, 61, 62, 63, 64)]
        wid = make_watch(uid, dates=",".join(dates))
        assert worker.run_user(uid)["attempts"] == 18
        FakeSearcher.calls.clear()
        assert worker.run_user(uid) == {"skipped": "nothing due today"} and not FakeSearcher.calls
        assert worker.run_user(uid, watch_ids={wid})["attempts"] == 18            # Search now: all


def test_habit_slots_are_a_personal_routine():
    from app.worker import habit_slots, plan_slots
    mon, sat = dt.date(2027, 1, 4), dt.date(2027, 1, 9)
    a1 = habit_slots(1, mon, rng=random.Random(1))
    a2 = habit_slots(1, dt.date(2027, 1, 5), rng=random.Random(2))
    assert 2 <= len(a1) <= 3 and len(a1) == len(a2)
    assert [t.hour for t in a1] == [t.hour for t in a2] or all(
        abs((x - y.replace(day=x.day)).total_seconds()) <= 45 * 60 for x, y in zip(a1, a2))   # same habits
    people = [habit_slots(u, mon, rng=random.Random(0)) for u in range(1, 60)]
    assert len({tuple(t.hour for t in p) for p in people}) > 3                   # different people
    assert sum(any(t.hour >= 18 for t in p) for p in people) > 40               # most look in the evening
    wk = habit_slots(1, sat, rng=random.Random(1))
    assert min(t.hour * 60 + t.minute for t in wk) >= 8 * 60 + 25               # no early mornings on Saturday
    plan = plan_slots(range(1, 9), mon, dt.datetime(2027, 1, 4, 0, 0), rng=random.Random(3))
    ts = [t for t, _ in plan]
    assert all(b - a >= dt.timedelta(minutes=20) for a, b in zip(ts, ts[1:]))
    assert plan_slots([1], mon, dt.datetime(2027, 1, 4, 23, 59)) == []


def test_one_visit_run_stops_and_the_next_check_in_continues(worker, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "visit_searches", (4, 4))
    monkeypatch.setattr(settings, "rotate", True)
    uid = make_user()
    today = dt.date.today()
    dates = [(today + dt.timedelta(days=n)).isoformat() for n in range(3, 10)]  # 7 near dates: 14 searches
    make_watch(uid, dates=",".join(dates))
    first = worker.run_user(uid, one_visit=True)
    assert first["attempts"] == 4 and not first["cancelled"]
    note = db.one("SELECT note FROM runs ORDER BY id DESC LIMIT 1")["note"]
    assert "the rest at the next check-in" in note
    worker.paused.discard(uid)
    FakeSearcher.calls.clear()
    second = worker.run_user(uid, one_visit=True)
    searched = {str(c[2]) for c in FakeSearcher.calls}
    assert second["attempts"] == 4 and len(searched) == 2                      # two more dates, none repeated
    assert len(db.query("SELECT * FROM searched")) == 4
