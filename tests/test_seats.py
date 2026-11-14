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
    legroom_any = {**seats.DEFAULT_PREFS, "position": "any", "no_middle": False}
    pick = seats.choose(MAP, legroom_any, "priority")
    assert pick["type"] == "extra" and pick["seat"] == "1E"            # front-most Extra Legroom
    assert seats.choose(MAP, legroom_any, "plus_premier")["type"] != "extra"   # not free for that card
    assert seats.choose(MAP, legroom_any, "priority", taken={"1E"})["seat"] != "1E"
    paid = [{**x, "price": 39.0} for x in MAP]
    assert seats.choose(paid, window, "priority") is None             # never anything that costs


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


def test_watch_form_saves_seat_preferences(client):
    from app import db
    from tests.conftest import future
    from tests.test_web import admin_client, watch_form
    t = admin_client(client)
    data = {**watch_form(t), "seat_auto": "1", "booking_conf": "abc12-3", "booking_first": "Pat",
            "booking_last": "Lee", "seat_status": "priority", "seat_position": "aisle", "seat_exit_row": "1",
            "booked_out_date": future(10), "booked_out_time": "13:15", "booked_out_from": "lir"}
    loc = client.post("/watches", data=data, follow_redirects=False).headers["location"].split("?")[0]
    w = db.one("SELECT * FROM watches WHERE id=?", (int(loc.rstrip("/").split("/")[-1]),))
    assert (w["seat_auto"], w["booking_conf"], w["seat_status"]) == (1, "ABC123", "priority")
    p = json.loads(w["seat_prefs"])
    assert p["position"] == "aisle" and p["exit_row"] and not p["extra_legroom"]
    b = json.loads(w["booked"])[0]
    assert b["time"] == "13:15" and b["from"] == "LIR" and b["cash"] is None   # seats only: no price needed
    edit = client.get(loc + "/edit").text
    assert 'value="ABC123"' in edit and 'value="priority" selected' in edit
    bad = {**data, "booked_out_time": ""}
    assert "departure time" in client.post("/watches", data=bad).text
    bad = {**data, "booking_conf": ""}
    assert "confirmation number" in client.post("/watches", data=bad).text


def _seat_watch(client, date, time_="13:15", origin="LIR"):
    from app import db
    from tests.test_web import admin_client, watch_form
    t = admin_client(client)
    data = {**watch_form(t), "seat_auto": "1", "booking_conf": "ABC123", "booking_first": "Pat",
            "booking_last": "Lee", "seat_status": "priority", "seat_position": "window",
            "booked_out_date": date, "booked_out_time": time_, "booked_out_from": origin}
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
