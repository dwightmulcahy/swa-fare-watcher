from app import db, swa
from tests.conftest import FakeSearcher, future
from tests.test_calendar import CalendarSearcher
from tests.test_roundtrip import flight, rt
from tests.test_web import admin_client, watch_form
from tests.test_worker import make_user, make_watch


def test_airports_and_routes():
    assert swa.airports(" sjo, LIR/sjo ") == ["SJO", "LIR"]
    w = {"origin": "SJO,LIR", "destination": "HOU,AUS", "dates": future(10), "days_from": None,
         "days_to": None, "weekdays": None}
    assert swa.routes(w) == [("SJO", "HOU"), ("SJO", "AUS"), ("LIR", "HOU"), ("LIR", "AUS")]
    assert swa.routes(w, "ret")[0] == ("HOU", "SJO")
    assert swa.route_label(w) == "SJO/LIR→HOU/AUS" and swa.route_label(w, "ret") == "HOU/AUS→SJO/LIR"
    assert swa.searches_per_run(w) == 2 * 4


def test_form_accepts_several_airports(client):
    token = admin_client(client)
    r = client.post("/watches", data=watch_form(token, origin="sjo, lir", destination="hou"),
                    follow_redirects=False)
    assert r.status_code == 303
    w = db.one("SELECT * FROM watches ORDER BY id DESC")
    assert (w["origin"], w["destination"], w["name"]) == ("SJO,LIR", "HOU", "SJO/LIR→HOU")
    page = client.get(f"/watches/{w['id']}").text
    assert "SJO/LIR" in page and "up to 8 searches" in page  # 2 dates × 2 pairs × cash+points


def test_form_rejects_bad_airport_lists(client):
    token = admin_client(client)
    for origin, msg in (("SJ0", "3-letter"), ("AUS,DAL,HOU,SAT", "at most 3"), ("BWI", "must differ")):
        r = client.post("/watches", data=watch_form(token, origin=origin), follow_redirects=False)
        assert r.status_code == 400 and msg in r.text, origin


def tagged(key, cash, points, frm, to):
    return {**flight(key, cash, points), "origin": frm, "dest": to}


def test_round_trip_can_mix_airports_unless_told_not_to():
    w = rt(origin="SJO,LIR", destination="HOU", dates="2026-12-18", nights_min=2, nights_max=2)
    legs = {"out": {"2026-12-18": [tagged("A", 150, 12000, "SJO", "HOU"), tagged("B", 100, 9000, "LIR", "HOU")]},
            "ret": {"2026-12-20": [tagged("C", 90, 7000, "HOU", "SJO"), tagged("D", 160, 13000, "HOU", "LIR")]}}
    (t,) = swa.roundtrips(w, legs)
    assert (t["cash"], t["out_route"], t["ret_route"]) == (190, "LIR→HOU", "HOU→SJO")
    assert "LIR" in t["url"] and "tripType=oneway" in t["url"] and "SJO" in t["ret_url"]
    (t,) = swa.roundtrips({**w, "same_airports": 1}, legs)
    assert t["cash"] == 240 and (t["out_route"], t["ret_route"]) in (("SJO→HOU", "HOU→SJO"), ("LIR→HOU", "HOU→LIR"))
    assert "tripType=roundtrip" in t["url"] and t["ret_url"] is None


def test_calendar_routes_skip_pairs_that_cannot_make_it():
    w = rt(trip_type="oneway", origin="SJO,LIR", destination="HOU")
    cal = {"out": {("SJO", "HOU"): {"2026-12-18": {"cash": 400, "points": 40000}},
                   ("LIR", "HOU"): {"2026-12-18": {"cash": 150, "points": 12000}}}}
    assert swa.calendar_routes(w, cal, [("out", "2026-12-18")]) == {("out", "2026-12-18"): [("LIR", "HOU")]}
    cal["out"][("SJO", "HOU")] = None   # unreadable: searched anyway
    assert len(swa.calendar_routes(w, cal, [("out", "2026-12-18")])[("out", "2026-12-18")]) == 2


def test_worker_searches_each_airport_pair(worker, sent):
    uid = make_user()
    wid = make_watch(uid, origin="AUS,DAL", destination="BWI", dates=future(10))
    res = worker.run_user(uid)
    assert sorted({(c[0], c[1]) for c in FakeSearcher.calls}) == [("AUS", "BWI"), ("DAL", "BWI")]
    assert res["attempts"] == 4 and res["hits"] == 2           # same flight key, but different airports
    fares = db.query("SELECT origin, dest FROM fares WHERE watch_id=?", (wid,))
    assert {(f["origin"], f["dest"]) for f in fares} == {("AUS", "BWI"), ("DAL", "BWI")}
    assert "DAL" in sent[0][2] and "originationAirportCode=DAL" in sent[0][2]


def test_worker_calendar_per_pair(worker, sent):
    d = future(10)
    CalendarSearcher.cal_calls = []
    CalendarSearcher.lows = {"USD": {d: 190}, "POINTS": {d: 12000}}
    worker.searcher_factory = CalendarSearcher
    uid = make_user()
    make_watch(uid, origin="AUS,DAL", destination="BWI", dates=d)
    worker.run_user(uid)
    assert {(c[0], c[1]) for c in CalendarSearcher.cal_calls} == {("AUS", "BWI"), ("DAL", "BWI")}
    assert len(CalendarSearcher.cal_calls) == 4


def test_calendar_loads_both_currencies_back_to_back(worker, sent):
    d = future(10)
    CalendarSearcher.cal_calls = []
    CalendarSearcher.lows = {"USD": {d: 190}, "POINTS": {d: 12000}}
    worker.searcher_factory = CalendarSearcher
    uid = make_user()
    make_watch(uid, origin="AUS,DAL", destination="BWI", dates=d)
    worker.run_user(uid)
    calls = CalendarSearcher.cal_calls
    for a, b in zip(calls[::2], calls[1::2]):       # same route and month, cash and points
        assert a[:3] == b[:3] and {a[3], b[3]} == {"USD", "POINTS"}
