import datetime as dt
import json
from pathlib import Path

from app import seats

LABELS = (Path(__file__).parent / "fake_swa" / "air" / "seat" / "labels.txt").read_text().splitlines()
MAP = [seats.parse_seat(x) for x in LABELS]


def test_parse_real_seat_labels():
    assert all(MAP) and len(MAP) == 112
    s = seats.parse_seat("Seat 5E  $0. (Chargeable, Front cabin, Extra leg room, Middle)")
    assert s["seat"] == "5E" and s["row"] == 5 and s["price"] == 0 and s["available"]
    assert s["type"] == "extra" and "middle" in s["features"] and not s["selected"]
    cur = next(x for x in MAP if x["selected"])
    assert cur["seat"] == "11F" and cur["type"] == "preferred" and not cur["available"]
    assert "window without window view" in cur["features"]
    assert seats.parse_seat("Seat 9A  $49.99. (Chargeable, Preferred, Window)")["price"] == 49.99
    assert seats.parse_seat("Dwight M. Seat: 11F, checked") is None
    assert sum(x["available"] for x in MAP) == 34


def test_choose_by_preferences_and_status():
    cur = next(x for x in MAP if x["selected"])
    window = dict(seats.DEFAULT_PREFS)
    best = seats.choose(MAP, window, "priority")
    assert best["seat"] == "9A" or "window" in best["features"]      # a real window, never a middle
    assert "middle" not in best["features"] and seats.better(best, cur, window)
    legroom_any = {"position": "any", "order": ["extra_legroom", "front"]}
    pick = seats.choose(MAP, legroom_any, "priority")
    assert pick["type"] == "extra" and pick["seat"] == "1E"            # front-most Extra Legroom
    assert seats.choose(MAP, legroom_any, "plus_premier")["type"] != "extra"   # not free for that card
    assert seats.choose(MAP, legroom_any, "priority", taken={"1E"})["seat"] != "1E"
    paid = [{**x, "price": 39.0} for x in MAP]
    assert seats.choose(paid, window, "priority") is None             # never anything that costs


def test_order_decides_which_wish_wins():
    legroom_first = {"position": "window", "order": ["extra_legroom", "seat_type", "front"]}
    window_first = {"position": "window", "order": ["seat_type", "extra_legroom", "front"]}
    a = seats.choose(MAP, legroom_first, "priority")
    b = seats.choose(MAP, window_first, "priority")
    assert a["type"] == "extra" and "window" in b["features"]
    assert seats.fit(a, legroom_first) > seats.fit(b, legroom_first)
    assert seats.fit(b, window_first) > seats.fit(a, window_first)
    perfect = {"seat": "1F", "row": 1, "type": "extra", "features": {"window", "exit row"}, "price": 0}
    assert round(seats.fit(perfect, window_first)) == 100
    far = {**perfect, "seat": "3F", "row": 3}
    assert not seats.better(far, perfect, {"position": "window", "order": ["seat_type"]})  # rows alone don't move


def test_old_preferences_become_an_order():
    old = {"seat_prefs": json.dumps({"position": "aisle", "extra_legroom": True, "front": False, "no_middle": True,
                                     "window_view": False, "exit_row": True, "bulkhead": False})}
    p = seats.prefs_of(old)
    assert p["order"] == ["no_middle", "extra_legroom", "seat_type", "exit_row"] and p["exit_row"] and not p["front"]
    assert seats.prefs_of({"seat_prefs": None})["order"] == seats.DEFAULT_ORDER
    anyp = seats.prefs_of({"seat_prefs": json.dumps({"position": "any", "order": ["seat_type", "front", "bogus"]})})
    assert anyp["order"] == ["front"]


def test_ranked_lists_free_seats_best_first():
    prefs = dict(seats.DEFAULT_PREFS)
    r = seats.ranked(MAP, prefs, "priority")
    assert r[0] == seats.choose(MAP, prefs, "priority") and len(r) > 6
    assert all(x["price"] == 0 and x["available"] for x in r)
    sc = [seats.score(x, prefs) for x in r]
    assert sc == sorted(sc, reverse=True)
    assert all(x["type"] != "extra" for x in seats.ranked(MAP, prefs, "plus_premier"))


def test_seat_email_shows_scored_options_even_when_unchanged():
    from app import mailer
    report = [{"flight": "Flight 1 LIR to HOU", "passenger": "Pat Lee",
               "current": {"seat": "1F", "describe": "1F (Extra Legroom, Window)", "price": 0, "score": 69.0},
               "options": [{"seat": "9A", "describe": "9A (Preferred, Window)", "price": 0, "score": 33.0}],
               "picked": None}]
    body = mailer.render_seat_result("Trip: outbound flight", [], "no better free seat was offered", True,
                                     "https://x/", report)
    assert "Pat Lee" in body and "1F (Extra Legroom, Window)" in body and "your seat" in body
    assert "9A (Preferred, Window)" in body and "33.0" in body and "chosen" not in body
    report[0]["picked"] = "9A"
    assert "chosen" in mailer.render_seat_result("T", [], "", True, "https://x/", report)


def test_no_change_when_current_seat_is_as_good():
    prefs = {**seats.DEFAULT_PREFS, "position": "window"}
    great = seats.parse_seat("Seat 1F Selected for passenger number 1 X. $0. (Chargeable, Front cabin, "
                             "Bulkhead seat, Extra leg room, Window) unavailable")
    pick = seats.choose(MAP, prefs, "priority")
    assert not seats.better(pick, great, prefs)
    assert seats.describe(great) == "1F (Extra Legroom, Window, bulkhead)"


def test_opens_48_hours_before_local_departure():
    t = seats.opens_at("2026-11-14", "13:15", "LIR")             # Costa Rica, UTC-6
    assert t == dt.datetime(2026, 11, 12, 19, 15, tzinfo=dt.timezone.utc)
    assert seats.opens_at("2026-11-14", "13:15", "ZZZ") is None
    assert seats.airport_tz("HOU") == "America/Chicago"


def test_seat_watch_form_saves_preferences(client):
    from app import db
    from tests.conftest import future
    from tests.test_web import admin_client
    t = admin_client(client)
    page = client.get("/watches/new?kind=seats").text
    assert 'name="seat_status"' in page and "max_price" not in page and "out_cash" not in page
    data = {"csrf": t, "kind": "seats", "booking_conf": "abc12-3", "booking_first": "Pat", "booking_last": "Lee",
            "origin": "LIR", "destination": "HOU", "trip_type": "oneway", "out_date": future(10),
            "out_time": "13:15", "seat_status": "priority", "seat_position": "aisle", "rank_exit_row": "1",
            "rank_no_middle": "2", "rank_front": "3",
            "active": "1"}
    loc = client.post("/watches", data=data, follow_redirects=False).headers["location"].split("?")[0]
    w = db.one("SELECT * FROM watches WHERE id=?", (int(loc.rstrip("/").split("/")[-1]),))
    assert (w["kind"], w["seat_auto"], w["booking_conf"], w["seat_status"]) == ("seats", 1, "ABC123", "priority")
    p = json.loads(w["seat_prefs"])
    assert p == {"position": "aisle", "order": ["exit_row", "no_middle", "front"]}
    b = json.loads(w["booked"])[0]
    assert b["time"] == "13:15" and b["from"] == "LIR"
    edit = client.get(loc + "/edit").text
    assert 'value="ABC123"' in edit and 'value="priority" selected' in edit
    assert edit.index('name="rank_exit_row"') < edit.index('name="rank_no_middle"') < edit.index('name="rank_front"')
    assert edit.index('name="rank_front"') < edit.index('name="rank_extra_legroom"')    # off ones last
    assert "departure time" in client.post("/watches", data={**data, "out_time": ""}).text
    assert "confirmation number" in client.post("/watches", data={**data, "booking_conf": ""}).text


def _seat_watch(client, date, time_="13:15", origin="LIR"):
    from app import db
    from tests.test_web import admin_client
    t = admin_client(client)
    data = {"csrf": t, "kind": "seats", "booking_conf": "ABC123", "booking_first": "Pat", "booking_last": "Lee",
            "origin": origin, "destination": "HOU", "trip_type": "oneway", "out_date": date, "out_time": time_,
            "seat_status": "priority", "seat_position": "window", "active": "1"}
    loc = client.post("/watches", data=data, follow_redirects=False).headers["location"].split("?")[0]
    with db.tx() as con:
        con.execute("UPDATE users SET email='pat@example.com' WHERE username='admin'")
    return int(loc.rstrip("/").split("/")[-1]), db


def test_saving_a_watch_schedules_seat_selection(client):
    from tests.conftest import future
    wid, db = _seat_watch(client, future(10))
    jobs = db.query("SELECT * FROM seat_jobs WHERE watch_id=?", (wid,))
    assert len(jobs) == 1 and jobs[0]["status"] == "pending" and jobs[0]["leg"] == "out"
    want = seats.opens_at(future(10), "13:15", "LIR").isoformat(timespec="seconds")
    assert jobs[0]["opens_at"] == want
    assert "Seat selection" in client.get(f"/watches/{wid}").text and "scheduled" in client.get(f"/watches/{wid}").text
    with db.tx() as con:
        con.execute("UPDATE seat_jobs SET status='done' WHERE watch_id=?", (wid,))
    db.sync_seat_jobs(wid)                                   # a job already run isn't planned again
    assert [j["status"] for j in db.query("SELECT status FROM seat_jobs WHERE watch_id=?", (wid,))] == ["done"]


def test_seat_job_runs_when_due_and_emails(client, worker, sent):
    from tests.conftest import future
    wid, db = _seat_watch(client, future(10))
    now = seats.opens_at(future(10), "13:15", "LIR")
    assert worker.due_seat_jobs(now - dt.timedelta(minutes=10)) == []
    job = worker.due_seat_jobs(now - dt.timedelta(minutes=2))[0]

    class Browser:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        human, page, humanize = None, None, False

    class Picker:
        def __init__(self, b, conf, first, last, prefs, status, user_id=None):
            assert (conf, first, last, status) == ("ABC123", "Pat", "Lee", "priority")
        report = [{"flight": "", "passenger": "Pat Lee", "current": None, "picked": "9A",
                   "options": [{"seat": "9A", "describe": "9A (Preferred, Window)", "price": 0, "score": 21.0}]}]
        def run(self, until, sleep=None, **kw):
            return [("11F", "9A (Preferred, Window)")], "confirmed"
    import app.seat_picker as sp
    real = sp.SeatPicker
    sp.SeatPicker = Picker
    try:
        status, changes, note = worker.run_seat_job(dict(job), picker_factory=Browser)
    finally:
        sp.SeatPicker = real
    assert status == "done" and changes[0][1].startswith("9A")
    assert db.one("SELECT status FROM seat_jobs WHERE id=?", (job["id"],))["status"] == "done"
    assert "Seat selected" in sent[-1][1] and "9A (Preferred, Window)" in sent[-1][2]
    assert "21.0" in sent[-1][2] and "chosen" in sent[-1][2]


def test_seat_job_that_would_charge_reports_and_changes_nothing(client, worker, sent):
    import app.seat_picker as sp
    from tests.conftest import future
    wid, db = _seat_watch(client, future(10))
    job = db.query("SELECT j.*, w.user_id FROM seat_jobs j JOIN watches w ON w.id=j.watch_id")[0]

    class Browser:
        def __enter__(self): return self
        def __exit__(self, *a): pass

    class Picker:
        def __init__(self, *a, **k): pass
        def run(self, *a, **k): raise sp.NotFree("the review page shows amount due 39.00")
    real, sp.SeatPicker = sp.SeatPicker, Picker
    try:
        status, _, note = worker.run_seat_job(dict(job), picker_factory=Browser)
    finally:
        sp.SeatPicker = real
    assert status == "failed" and "before paying anything" in note and "needs you" in sent[-1][1]


def test_new_watch_starts_with_a_choice_of_kind(client):
    from tests.test_web import admin_client
    admin_client(client)
    page = client.get("/watches/new").text
    for k in ("fare", "rebook", "seats"):
        assert f'href="/watches/new?kind={k}"' in page


def test_seat_watches_dont_search_or_count_against_limits(client, worker):
    from tests.conftest import FakeSearcher, future
    wid, db = _seat_watch(client, future(10))
    uid = db.one("SELECT user_id FROM watches WHERE id=?", (wid,))["user_id"]
    assert worker.run_user(uid) == {"skipped": "no active watches"}
    assert FakeSearcher.calls == [] and uid not in [u for u in worker.eligible_users()]


def test_old_booked_fares_become_rebook_watches():
    from app import db
    from tests.test_worker import make_user, make_watch
    uid = make_user()
    make_watch(uid, name="Trip", booked=json.dumps([{"leg": "out", "date": "2027-02-01", "cash": 200,
                                                           "points": None, "flights": "9"}]))
    with db.tx() as con:
        con.execute("DELETE FROM app_state WHERE key='kinds_split'")
    db.init()
    rows = db.query("SELECT * FROM watches WHERE user_id=? ORDER BY id", (uid,))
    assert [r["kind"] for r in rows] == ["fare", "rebook"] and rows[0]["booked"] is None
    assert rows[1]["name"] == "Trip (booked)" and json.loads(rows[1]["booked"])[0]["cash"] == 200
    db.init()                                   # only once
    assert len(db.query("SELECT * FROM watches WHERE user_id=?", (uid,))) == 2


def test_pages_render_for_each_kind(client):
    from tests.conftest import future
    wid, db = _seat_watch(client, future(10))
    page = client.get(f"/watches/{wid}").text
    assert "Conf. ABC123" in page and "Search now" not in page and "Seat selection" in page
    dash = client.get("/").text
    assert "kind-seats" in dash and "Scheduled" in dash


TRIP = """Departing
Sat, November 14, 2026
Flight
WN567
Boeing 737-800 (subject to change)
Departs
Liberia (Guanacaste)
1:15
PM
Liberia (Guanacaste), Costa Rica - LIR
On Time
Arrives
Houston
4:45
PM
Houston (Hobby), TX - HOU
Seat Assignments
Returning
Sat, November 21, 2026
Flight
WN1203
Departs
Houston
9:05
AM
Houston (Hobby), TX - HOU
Arrives
Dallas
10:10
AM
Dallas (Love Field), TX - DAL
Flight
WN4972
Departs
Dallas
12:40
PM
Dallas (Love Field), TX - DAL
Arrives
Liberia
4:30
PM
Liberia (Guanacaste), Costa Rica - LIR
"""


def test_parse_trip_page_flights():
    from app.seat_picker import parse_trip
    legs = parse_trip(TRIP)
    assert legs == [
        {"leg": "out", "date": "2026-11-14", "time": "13:15", "from": "LIR", "to": "HOU", "flights": "567"},
        {"leg": "ret", "date": "2026-11-21", "time": "09:05", "from": "HOU", "to": "LIR", "flights": "1203/4972"},
    ]
    assert parse_trip("nothing here") == []
    assert parse_trip("Departing\nSun, November 15, 2026\nDeparts\nX\n12:05\nAM\nX - LIR\nArrives\nY - HOU")[0]["time"] == "00:05"


def test_seat_watch_with_just_the_booking_looks_it_up(client, worker, monkeypatch):
    from app import db
    from tests.test_web import admin_client
    t = admin_client(client)
    started = []
    monkeypatch.setattr(worker, "lookup_booking", lambda wid: started.append(wid))
    data = {"csrf": t, "kind": "seats", "booking_conf": "ABC123", "booking_first": "Pat", "booking_last": "Lee",
            "seat_status": "priority", "seat_position": "window", "active": "1"}
    loc = client.post("/watches", data=data, follow_redirects=False).headers["location"].split("?")[0]
    wid = int(loc.rstrip("/").split("/")[-1])
    import time as _t
    for _ in range(50):
        if started:
            break
        _t.sleep(0.02)
    w = db.one("SELECT * FROM watches WHERE id=?", (wid,))
    assert started == [wid] and w["booked"] is None and w["name"] == "Seats ABC123"
    page = client.get(loc).text
    assert "Looking up the booking" in page and "Look up the booking again" in page


def test_lookup_booking_fills_the_watch_and_plans_seats(client, worker):
    from tests.test_web import admin_client
    t = admin_client(client)
    worker.lookup_booking = lambda wid: None          # don't start one from the web request
    data = {"csrf": t, "kind": "seats", "booking_conf": "ABC123", "booking_first": "Pat", "booking_last": "Lee",
            "seat_status": "priority", "seat_position": "window", "active": "1"}
    loc = client.post("/watches", data=data, follow_redirects=False).headers["location"].split("?")[0]
    wid = int(loc.rstrip("/").split("/")[-1])
    from app import db
    from app.worker import Worker
    future_trip = TRIP.replace("2026", "2030")

    class Page:
        def inner_text(self, sel): return future_trip

    class Browser:
        humanize, human, page = False, None, Page()
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def warm_up(self): pass
    import app.seat_picker as sp
    real = sp.SeatPicker.open_reservation
    sp.SeatPicker.open_reservation = lambda self: None
    try:
        legs = Worker.lookup_booking(worker, wid, browser_factory=Browser)
    finally:
        sp.SeatPicker.open_reservation = real
    assert [x["leg"] for x in legs] == ["out", "ret"]
    w = db.one("SELECT * FROM watches WHERE id=?", (wid,))
    assert (w["origin"], w["destination"], w["trip_type"]) == ("LIR", "HOU", "roundtrip")
    assert len(db.query("SELECT * FROM seat_jobs WHERE watch_id=?", (wid,))) == 2
    assert w["last_status"] == "Booking read: 2 flight(s)"
