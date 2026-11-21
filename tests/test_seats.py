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


def test_choose_by_priorities_and_status():
    cur = next(x for x in MAP if x["selected"])
    prefs = seats.normalize({})
    best = seats.choose(MAP, prefs, "priority")
    assert "middle" not in best["features"] and seats.better(best, cur, prefs)
    assert seats.priority(best, prefs)[0] == min(seats.priority(x, prefs)[0] for x in seats.ranked(MAP, prefs, "priority"))
    assert seats.choose(MAP, prefs, "premier")["type"] != "extra"          # not free for that card
    assert seats.choose(MAP, prefs, "priority", taken={best["seat"]})["seat"] != best["seat"]
    paid = [{**x, "price": 39.0} for x in MAP]
    assert seats.choose(paid, prefs, "priority") is None                    # never anything that costs


def test_exit_window_with_no_seat_in_front():
    import copy
    plane = seats.annotate([x for x in copy.deepcopy(MAP) if x["seat"] not in ("14A", "14F")])  # door space
    open_ = {x["seat"] for x in plane if seats.OPEN_FRONT in x["features"]}
    assert open_ == {"15A", "15F"}                       # not row 1 (nothing ahead) or full rows
    assert not any(seats.OPEN_FRONT in x["features"] for x in seats.annotate(copy.deepcopy(MAP)))
    s15 = next(x for x in plane if x["seat"] == "15A")
    assert "no seat in front" in seats.describe(s15)
    free = [{**x, "available": True, "price": 0} if x["seat"] in ("15A", "2A") else x for x in plane]
    assert seats.choose(free, seats.normalize({}), "priority")["seat"] == "15A"


def test_old_preferences_become_priorities():
    old = {"seat_prefs": json.dumps({"position": "aisle", "order": ["no_middle", "extra_legroom", "seat_type"]})}
    p = seats.prefs_of(old)
    assert p["migrated"] and p["priorities"]["middle"] is None              # "never a middle" kept
    pri = p["priorities"]
    assert pri["extra_aisle"] < pri["extra_window"] < pri["std_aisle"] < pri["std_window"]
    flags = seats.prefs_of({"seat_prefs": json.dumps({"position": "window", "extra_legroom": True, "no_middle": False,
                                                      "exit_row": False})})
    assert flags["migrated"] and flags["priorities"]["middle"] is not None   # wasn't excluded: not switched off
    assert seats.prefs_of({"seat_prefs": None})["priorities"] == seats.DEFAULT_PRIORITIES


def test_ranked_lists_free_seats_best_first():
    prefs = dict(seats.DEFAULT_PREFS)
    r = seats.ranked(MAP, prefs, "priority")
    assert r[0] == seats.choose(MAP, prefs, "priority") and len(r) > 6
    assert all(x["price"] == 0 and x["available"] for x in r)
    keys = [seats.key(x, prefs) for x in r]
    assert keys == sorted(keys)
    assert all(x["type"] != "extra" for x in seats.ranked(MAP, prefs, "premier"))


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
            "out_time": "13:15", "seat_status": "priority", "seat_position": "aisle", "prio_exit_aisle": "1",
            "prio_extra_aisle": "1", "prio_std_aisle": "2", "prio_std_window": "3", "tiebreak_order": "view,legroom",
            "tb_view": "1", "tb_legroom": "1", "seat_explain": "1", "active": "1"}
    loc = client.post("/watches", data=data, follow_redirects=False).headers["location"].split("?")[0]
    w = db.one("SELECT * FROM watches WHERE id=?", (int(loc.rstrip("/").split("/")[-1]),))
    assert (w["kind"], w["seat_auto"], w["booking_conf"], w["seat_status"]) == ("seats", 1, "ABC123", "priority")
    p = json.loads(w["seat_prefs"])
    assert p["priorities"] == {"exit_window_open": None, "exit_window": None, "extra_window": None,
                               "bulkhead_window": None, "exit_aisle": 1, "extra_aisle": 1, "bulkhead_aisle": None,
                               "std_window": 3, "std_aisle": 2, "middle": None}
    assert p["tiebreak"] == ["view", "legroom"] and p["explain"] and not p["fallback"] and p["version"] == 2
    b = json.loads(w["booked"])[0]
    assert b["time"] == "13:15" and b["from"] == "LIR"
    edit = client.get(loc + "/edit").text
    assert 'value="ABC123"' in edit and 'value="priority" selected' in edit
    assert edit.index('name="prio_exit_aisle"') < edit.index('name="prio_std_aisle"') < edit.index('name="prio_std_window"')
    assert edit.index('name="prio_std_window"') < edit.index('name="prio_middle"')        # off ones last
    assert "departure time" in client.post("/watches", data={**data, "out_time": ""}).text
    assert "confirmation number" in client.post("/watches", data={**data, "booking_conf": ""}).text


def _seat_watch(client, date, time_="13:15", origin="LIR", status="premier"):
    from app import db
    from tests.test_web import admin_client
    t = admin_client(client)
    data = {"csrf": t, "kind": "seats", "booking_conf": "ABC123", "booking_first": "Pat", "booking_last": "Lee",
            "origin": origin, "destination": "HOU", "trip_type": "oneway", "out_date": date, "out_time": time_,
            "seat_status": status, "seat_position": "window", "active": "1"}
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
        def __init__(self, b, conf, first, last, prefs, status, user_id=None, window=True):
            assert (conf, first, last, status, window) == ("ABC123", "Pat", "Lee", "premier", True)
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
    assert status == "watching" and changes[0][1].startswith("9A")        # 21/100: keeps looking
    j = db.one("SELECT * FROM seat_jobs WHERE id=?", (job["id"],))
    assert j["status"] == "watching" and j["visits"] == 1 and json.loads(j["log"]) == [["11F", "9A (Preferred, Window)"]]
    assert "Seat selected" in sent[-1][1] and "9A (Preferred, Window)" in sent[-1][2]
    assert "21.0" in sent[-1][2] and "chosen" in sent[-1][2] and "Checking again" in sent[-1][2]
    assert "confirmationNumber=ABC123&amp;passengerFirstName=Pat&amp;passengerLastName=Lee" in sent[-1][2]


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
            "seat_status": "premier", "seat_position": "window", "active": "1"}
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
            "seat_status": "premier", "seat_position": "window", "active": "1"}
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
    real, real_cap = sp.SeatPicker.open_reservation, sp.SeatPicker.capture_maps
    sp.SeatPicker.open_reservation = lambda self: None
    sp.SeatPicker.capture_maps = lambda self: [{"flight": "Flight 1 of 2 LIR to HOU", "plane": "Boeing 737 MAX 8",
                                                "seats": seats.example_maps()["Boeing 737 MAX 8"][:12]}]
    try:
        legs = Worker.lookup_booking(worker, wid, browser_factory=Browser)
    finally:
        sp.SeatPicker.open_reservation, sp.SeatPicker.capture_maps = real, real_cap
    assert [x["leg"] for x in legs] == ["out", "ret"]
    w = db.one("SELECT * FROM watches WHERE id=?", (wid,))
    assert (w["origin"], w["destination"], w["trip_type"]) == ("LIR", "HOU", "roundtrip")
    assert len(db.query("SELECT * FROM seat_jobs WHERE watch_id=?", (wid,))) == 2
    assert w["last_status"] == "Booking read: 2 flight(s)"
    maps = json.loads(w["seat_maps"])                                   # the plane, read at creation
    assert maps[0]["plane"] == "Boeing 737 MAX 8" and maps[0]["read"] and len(maps[0]["seats"]) == 12
    edit = client.get(f"/watches/{wid}/edit").text
    assert "Your flight: Flight 1 of 2 LIR to HOU · Boeing 737 MAX 8" in edit
    db.save_seat_maps(wid, [{"flight": "Flight 1 of 2 LIR to HOU", "plane": "Boeing 737-800", "seats": []}])
    assert [m["plane"] for m in json.loads(db.one("SELECT seat_maps FROM watches WHERE id=?", (wid,))["seat_maps"])] \
        == ["Boeing 737-800"]                                             # merged by flight: the newer one


def test_manage_link_prefills_the_lookup():
    from app import seat_picker
    assert seat_picker.manage_url("ABC123", "Mary Ann", "Lee") == (
        "https://www.southwest.com/air/manage-reservation/index.html"
        "?confirmationNumber=ABC123&passengerFirstName=Mary+Ann&passengerLastName=Lee")
    assert seat_picker.manage_url(None, "Pat", "Lee") == seat_picker.MANAGE_URL


def test_next_seat_check_like_a_person(worker):
    import random
    utc = dt.timezone.utc
    departs = dt.datetime(2026, 11, 20, 18, 0, tzinfo=utc)
    rng = random.Random(1)
    gaps, late = [], []
    for _ in range(200):
        now = departs - dt.timedelta(hours=rng.uniform(13, 47))
        n = worker.next_seat_visit(now, departs, rng)
        local = n.astimezone()
        assert not (local.hour >= 23 or local.hour + local.minute / 60 < 6.5) or n > departs - dt.timedelta(hours=2)
        assert n <= departs - dt.timedelta(hours=1)
        gaps.append((n - now).total_seconds() / 3600)
    worker.SEAT_QUIET = (24, 0)                                # no nights, to compare gaps alone
    for _ in range(200):
        now = departs - dt.timedelta(hours=rng.uniform(3, 11))
        late.append((worker.next_seat_visit(now, departs, rng) - now).total_seconds() / 3600)
    assert min(gaps) >= 2 and max(late) <= 2                   # more often in the last 12 hours
    assert worker.next_seat_visit(departs - dt.timedelta(minutes=80), departs) is None


def _run_visit(worker, job, run, report):
    import app.seat_picker as sp

    class Browser:
        def __enter__(self): return self
        def __exit__(self, *a): pass

    class Picker:
        def __init__(self, *a, **k): self.report = []
        def run(self, until, **k):
            self.report = report
            return run(until)
    real, sp.SeatPicker = sp.SeatPicker, Picker
    try:
        return worker.run_seat_job(dict(job), picker_factory=Browser)
    finally:
        sp.SeatPicker = real


def test_seat_watch_keeps_checking_until_perfect_then_sends_a_summary(client, worker, sent):
    from tests.conftest import future
    wid, db = _seat_watch(client, future(10))
    opened = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1)).isoformat(timespec="seconds")
    with db.tx() as con:                       # selection opened a minute ago
        con.execute("UPDATE seat_jobs SET opens_at=? WHERE watch_id=?", (opened, wid))
    job = worker.due_seat_jobs()[0]
    meh = [{"current": {"seat": "11F", "describe": "11F", "price": 0, "score": 40.0}, "options": [], "picked": None}]
    untils = []
    status, _, note = _run_visit(worker, job, lambda u: (untils.append(u), ([], "no better"))[1], meh)
    assert status == "watching" and not sent and untils[0] > 0              # first visit waits at the opening
    j = db.one("SELECT j.*, w.user_id FROM seat_jobs j JOIN watches w ON w.id=j.watch_id WHERE j.id=?", (job["id"],))
    assert worker.due_seat_jobs(dt.datetime.fromisoformat(j["next_at"]) - dt.timedelta(minutes=1)) == []
    assert worker.due_seat_jobs(dt.datetime.fromisoformat(j["next_at"]))[0]["id"] == job["id"]
    best = [{"current": meh[0]["current"], "picked": "1F",
             "options": [{"seat": "1F", "describe": "1F (Extra Legroom, Window)", "price": 0, "score": 100.0,
                          "priority": 1, "label": "Priority 1: Extra-legroom window"}]}]
    status, _, note = _run_visit(worker, j, lambda u: (untils.append(u), ([("11F", "1F (Extra Legroom, Window)")],
                                                                          "confirmed"))[1], best)
    assert untils[1] == 0                                                   # later visits look once
    assert status == "done" and "top priority" in note and len(sent) == 1
    assert "Seat watch finished" in sent[-1][1] and "1F (Extra Legroom, Window)" in sent[-1][2]
    assert db.one("SELECT next_at FROM seat_jobs WHERE id=?", (job["id"],))["next_at"] is None


def test_seat_watch_gives_up_after_repeated_failures(client, worker, sent):
    from tests.conftest import future
    wid, db = _seat_watch(client, future(10))
    job = worker.due_seat_jobs(seats.opens_at(future(10), "13:15", "LIR"))[0]

    def boom(u):
        raise RuntimeError("page changed")
    q = "SELECT j.*, w.user_id FROM seat_jobs j JOIN watches w ON w.id=j.watch_id WHERE j.id=?"
    for want in ("watching", "watching", "failed"):
        status, _, note = _run_visit(worker, db.one(q, (job["id"],)), boom, [])
        assert status == want
    assert "3 times in a row" in note and len(sent) == 1 and "needs you" in sent[-1][1]


def test_watch_page_shows_the_next_seat_check(client):
    from tests.conftest import future
    wid, db = _seat_watch(client, future(10))
    opens = seats.opens_at(future(10), "13:15", "LIR")
    page = client.get(f"/watches/{wid}").text
    assert "Next check" in page and "seat selection opens" in page
    assert (opens - dt.timedelta(seconds=150)).astimezone().strftime("%a %b %-d, %H:%M") in page
    nxt = dt.datetime(2030, 1, 2, 15, 40, tzinfo=dt.timezone.utc)
    with db.tx() as con:
        con.execute("UPDATE seat_jobs SET status='watching', next_at=? WHERE watch_id=?", (nxt.isoformat(), wid))
    page = client.get(f"/watches/{wid}").text
    assert nxt.astimezone().strftime("%a %b %-d, %H:%M") in page and "checking for a better seat" in page
    with db.tx() as con:
        con.execute("UPDATE seat_jobs SET status='done', next_at=NULL WHERE watch_id=?", (wid,))
    assert "Nothing scheduled" in client.get(f"/watches/{wid}").text
    with db.tx() as con:
        con.execute("UPDATE watches SET active=0 WHERE id=?", (wid,))
    assert "Turn the watch back on" in client.get(f"/watches/{wid}").text


def test_restart_puts_interrupted_seat_checks_back_in_line(client, worker):
    from tests.conftest import future
    wid, db = _seat_watch(client, future(10))
    opens = seats.opens_at(future(10), "13:15", "LIR")
    job = worker.due_seat_jobs(opens)[0]
    with db.tx() as con:
        con.execute("UPDATE seat_jobs SET status='running' WHERE id=?", (job["id"],))
    worker.close_interrupted_runs()
    assert worker.due_seat_jobs(opens)[0]["id"] == job["id"]                  # pending again
    with db.tx() as con:
        con.execute("UPDATE seat_jobs SET status='running', visits=2 WHERE id=?", (job["id"],))
    worker.close_interrupted_runs()
    j = db.one("SELECT * FROM seat_jobs WHERE id=?", (job["id"],))
    assert j["status"] == "watching" and worker.due_seat_jobs(dt.datetime.now(dt.timezone.utc))
    old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=47.5)).isoformat(timespec="seconds")
    with db.tx() as con:
        con.execute("UPDATE seat_jobs SET status='running', opens_at=? WHERE id=?", (old, job["id"]))
    worker.close_interrupted_runs()
    assert db.one("SELECT status FROM seat_jobs WHERE id=?", (job["id"],))["status"] == "failed"



def test_status_decides_what_is_free_when():
    assert seats.allowed("a_list_preferred", window=False) == {"extra", "preferred", "standard"}
    assert seats.allowed("a_list", window=False) == {"preferred", "standard"}
    assert seats.allowed("a_list") == {"extra", "preferred", "standard"}
    assert seats.allowed("plus") == {"standard"} and seats.allowed("plus", window=False) == set()
    assert seats.allowed("plus_premier") == {"preferred", "standard"}          # old setting = Premier
    assert seats.free_early("a_list_preferred") and not seats.window_gain("a_list_preferred")
    assert seats.free_early("a_list") and seats.window_gain("a_list") == {"extra"}
    assert not seats.free_early("premier") and not seats.free_early("none")
    legroom = seats.normalize({"priorities": {"extra_window": 1, "extra_aisle": 1, "middle": 1, "std_window": 2,
                                              "std_aisle": 2}, "tiebreak": ["legroom"]})
    early = seats.choose(MAP, legroom, "a_list", window=False)
    assert early["type"] != "extra" and seats.choose(MAP, legroom, "a_list")["type"] == "extra"


def test_a_list_preferred_watch_checks_right_away_and_about_daily(client, worker):
    from tests.conftest import future
    wid, db = _seat_watch(client, future(20), status="a_list_preferred")
    now = dt.datetime.now(dt.timezone.utc)
    j = db.one("SELECT * FROM seat_jobs WHERE watch_id=?", (wid,))
    assert j["status"] == "watching" and worker.due_seat_jobs(now + dt.timedelta(seconds=5))
    meh = [{"current": {"seat": "11F", "describe": "11F", "price": 0, "score": 40.0}, "options": [], "picked": None}]
    untils, windows = [], []
    import app.seat_picker as sp
    real = sp.SeatPicker

    class Picker:
        def __init__(self, *a, window=True, **k):
            windows.append(window)
            self.report = meh
        def run(self, until, **k):
            untils.append(until)
            return [], "no better"

    class Browser:
        def __enter__(self): return self
        def __exit__(self, *a): pass
    sp.SeatPicker = Picker
    try:
        job = db.one("SELECT j.*, w.user_id FROM seat_jobs j JOIN watches w ON w.id=j.watch_id WHERE j.id=?", (j["id"],))
        status, _, note = worker.run_seat_job(dict(job), picker_factory=Browser)
    finally:
        sp.SeatPicker = real
    assert status == "watching" and untils == [0] and windows == [False]        # one look, booking seats
    nxt = dt.datetime.fromisoformat(db.one("SELECT next_at FROM seat_jobs WHERE id=?", (j["id"],))["next_at"])
    assert nxt - now >= dt.timedelta(hours=15)                                   # about a day later


def test_a_list_never_skips_the_48_hour_opening(worker):
    import random
    departs = dt.datetime(2027, 1, 10, 18, 0, tzinfo=dt.timezone.utc)
    opens = departs - dt.timedelta(hours=48, seconds=worker.SEAT_EARLY)
    now = opens - dt.timedelta(hours=10)
    assert worker.next_seat_visit(now, departs, random.Random(3), gain=True) == opens
    assert worker.next_seat_visit(now, departs, random.Random(3), gain=False) != opens



def test_international_flights_stop_earlier(worker):
    import random
    assert seats.international("LIR", "HOU") and not seats.international("AUS", "SJU")
    departs = dt.datetime(2027, 1, 10, 18, 0, tzinfo=dt.timezone.utc)
    now = departs - dt.timedelta(hours=3)
    worker.SEAT_QUIET = (24, 0)
    for seed in range(30):
        n = worker.next_seat_visit(now, departs, random.Random(seed), stop_before=worker.SEAT_STOP_BEFORE_INTL)
        assert n is None or departs - n > dt.timedelta(minutes=90)



def test_restart_replans_waiting_seat_jobs_for_new_rules(client, worker):
    from tests.conftest import future
    wid, db = _seat_watch(client, future(20))
    with db.tx() as con:
        con.execute("UPDATE watches SET seat_status='a_list_preferred' WHERE id=?", (wid,))
    assert db.one("SELECT status FROM seat_jobs WHERE watch_id=?", (wid,))["status"] == "pending"
    worker.close_interrupted_runs()
    assert db.one("SELECT status FROM seat_jobs WHERE watch_id=?", (wid,))["status"] == "watching"



def test_broken_seat_check_retries_soon_and_right_after_a_restart(client, worker):
    from tests.conftest import future
    wid, db = _seat_watch(client, future(20), status="a_list_preferred")
    j = db.one("SELECT j.*, w.user_id FROM seat_jobs j JOIN watches w ON w.id=j.watch_id WHERE w.id=?", (wid,))

    def boom(u):
        raise RuntimeError("seat 3A didn't stick (shows 7A)")
    now = dt.datetime.now(dt.timezone.utc)
    status, _, _ = _run_visit(worker, j, boom, [])
    nxt = dt.datetime.fromisoformat(db.one("SELECT next_at FROM seat_jobs WHERE id=?", (j["id"],))["next_at"])
    assert status == "watching" and nxt - now <= dt.timedelta(minutes=41)
    with db.tx() as con:
        con.execute("UPDATE seat_jobs SET next_at='2099-01-01T00:00:00+00:00' WHERE id=?", (j["id"],))
    worker.close_interrupted_runs()
    assert worker.due_seat_jobs(dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=5))


def test_watch_page_shows_the_current_seat_and_score(client, worker, sent):
    from tests.conftest import future
    wid, db = _seat_watch(client, future(20), status="a_list_preferred")
    j = db.one("SELECT j.*, w.user_id FROM seat_jobs j JOIN watches w ON w.id=j.watch_id WHERE w.id=?", (wid,))
    rep = [{"flight": "", "passenger": "Dwight M.", "picked": None,
            "current": {"seat": "7A", "describe": "7A (Preferred, Window)", "price": 0, "score": 62.4},
            "options": [{"seat": "9F", "describe": "9F (Preferred, Window)", "price": 0, "score": 60.0}]}]
    _run_visit(worker, j, lambda u: ([], "no better"), rep)
    page = client.get(f"/watches/{wid}").text
    assert "7A (Preferred, Window)" in page and "score 62" in page and "9F (Preferred, Window)" in page
    rep2 = [{**rep[0], "picked": "3A", "options": [{"seat": "3A", "describe": "3A (Extra Legroom, Window)",
                                                     "price": 0, "score": 100.0}]}]
    j = db.one("SELECT j.*, w.user_id FROM seat_jobs j JOIN watches w ON w.id=j.watch_id WHERE w.id=?", (wid,))
    _run_visit(worker, j, lambda u: ([("7A", "3A (Extra Legroom, Window)")], "confirmed"), rep2)
    page = client.get(f"/watches/{wid}").text
    assert "<b>3A (Extra Legroom, Window)</b>" in page and "score 100" in page
    assert "3A · score 100" in client.get("/").text                        # and on the dashboard


def test_plane_change_emails_the_owner(client, worker, sent):
    from tests.conftest import future
    wid, db = _seat_watch(client, future(20), status="a_list_preferred")
    w = db.one("SELECT * FROM watches WHERE id=?", (wid,))
    m = {"flight": "Flight 1 of 1 LIR to HOU", "seats": []}
    assert db.save_seat_maps(wid, [{**m, "plane": "Boeing 737-800"}]) == []          # first sighting: no alert
    assert db.save_seat_maps(wid, [{**m, "plane": None}]) == []                      # not named: keeps the last
    assert json.loads(db.one("SELECT seat_maps FROM watches WHERE id=?", (wid,))["seat_maps"])[0]["plane"] \
        == "Boeing 737-800"
    changes = db.save_seat_maps(wid, [{**m, "plane": "Boeing 737 MAX 8"}])
    assert changes == [("Flight 1 of 1 LIR to HOU", "Boeing 737-800", "Boeing 737 MAX 8")]
    worker.plane_changed(w, changes)
    assert "SWA plane change" in sent[-1][1] and "Boeing 737-800 → Boeing 737 MAX 8" in sent[-1][1]
    assert "manage-reservation/index.html?confirmationNumber=ABC123" in sent[-1][2]
    assert db.save_seat_maps(wid, [{**m, "plane": "Boeing 737 MAX 8"}]) == []        # told once


def test_seat_checks_use_their_own_signed_out_browser_and_poll_gently(client, worker):
    """Seat checks never use the fare-search browser (signed in to the search account), and the
    opening check reloads every 3-5 minutes."""
    from tests.conftest import future
    wid, db = _seat_watch(client, future(10))
    opened = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1)).isoformat(timespec="seconds")
    with db.tx() as con:
        con.execute("UPDATE seat_jobs SET opens_at=? WHERE watch_id=?", (opened, wid))
    job = worker.due_seat_jobs()[0]
    made, polls = [], []
    import app.seat_picker as sp

    class Browser:
        def __enter__(self): return self
        def __exit__(self, *a): pass

    class Picker:
        def __init__(self, *a, **k): self.report = []
        def run(self, until, poll=None, **k):
            polls.append(poll)
            return [], "no better"
    real_factory, real = worker.searcher_factory, sp.SeatPicker
    worker.searcher_factory = lambda state, profile_key=None: made.append((state, profile_key)) or Browser()
    sp.SeatPicker = Picker
    try:
        worker.run_seat_job(dict(job))
    finally:
        worker.searcher_factory, sp.SeatPicker = real_factory, real
    uid = job["user_id"]
    assert made == [(None, f"{uid}-seats")] and polls == [(180, 300)]
    assert worker.SEAT_WINDOW // 300 >= 4 and worker.SEAT_WINDOW // 180 <= 7             # 4-6 reloads


def test_seat_history_is_recorded_and_shown_on_the_watch(client, worker, sent):
    from tests.conftest import future
    wid, db = _seat_watch(client, future(10))
    opened = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1)).isoformat(timespec="seconds")
    with db.tx() as con:
        con.execute("UPDATE seat_jobs SET opens_at=? WHERE watch_id=?", (opened, wid))
    q = "SELECT j.*, w.user_id FROM seat_jobs j JOIN watches w ON w.id=j.watch_id WHERE j.id=?"
    job = worker.due_seat_jobs()[0]
    cur = {"seat": "11F", "describe": "11F (Window)", "price": 0, "score": 40.0, "priority": 3, "label": "Priority 3: Window"}
    alt = {"seat": "20C", "describe": "20C (Aisle)", "price": 0, "score": 30.0, "priority": 4}
    meh = [{"passenger": "Pat Q", "current": cur, "options": [alt], "picked": None}]
    for _ in range(3):
        _run_visit(worker, db.one(q, (job["id"],)), lambda u: ([], "no better"), meh)
    top = {"seat": "1F", "describe": "1F (Extra Legroom, Window)", "price": 0, "score": 100.0, "priority": 1,
           "label": "Priority 1: Extra-legroom window"}
    _run_visit(worker, db.one(q, (job["id"],)), lambda u: ([("11F", top["describe"])], "confirmed"),
               [{"passenger": "Pat Q", "current": cur, "options": [top], "picked": "1F"}])
    ev = [r["event"] for r in db.query("SELECT event FROM seat_history WHERE watch_id=? ORDER BY id", (wid,))]
    assert ev == ["first", "kept", "kept", "moved", "finished"]
    page = client.get(f"/watches/{wid}").text
    assert "Seat history" in page and "3 checks" in page and "from 11F (Window)" in page
    assert "best other free seat seen: 20C (Aisle)" in page and page.index("Finished") < page.index("First check")
