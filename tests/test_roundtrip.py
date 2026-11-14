import datetime as dt

from app import db, swa
from tests.conftest import FakeSearcher, future
from tests.test_web import admin_client, watch_form
from tests.test_worker import make_user, make_watch

TODAY = dt.date(2026, 11, 8)


def rt(**kw):
    base = {"trip_type": "roundtrip", "origin": "AUS", "destination": "BWI", "dates": "2026-12-18,2026-12-19", "days_from": None, "days_to": None,
            "weekdays": None, "ret_dates": None, "nights_min": 2, "nights_max": 3,
            "max_stops": 1, "max_duration_min": None, "depart_after": None, "depart_before": None,
            "ret_depart_after": None, "ret_depart_before": None, "max_price": 300, "max_points": 25000,
            "min_cpp": 1.3, "mix_payment": 1}
    return {**base, **kw}


def flight(key, cash, points, taxes=5.6, depart="09:00", stops=0):
    c = round((cash - taxes) / points * 100, 2) if cash and points else None
    return {"key": key, "flights": key, "depart": depart, "arrive": "12:00", "stops": stops, "duration": 180,
            "cash": cash, "points": points, "points_taxes": taxes, "cpp": c}


def test_return_dates_from_stay_length_and_pairs():
    w = rt()
    assert swa.return_dates(w, TODAY) == ["2026-12-20", "2026-12-21", "2026-12-22"]
    assert swa.trip_pairs(w, TODAY) == [("2026-12-18", "2026-12-20"), ("2026-12-18", "2026-12-21"),
                                        ("2026-12-19", "2026-12-21"), ("2026-12-19", "2026-12-22")]
    assert swa.searches_per_run(w, TODAY) == 2 * (2 + 3)  # each date searched once per direction


def test_explicit_return_dates_pair_with_every_earlier_departure():
    w = rt(ret_dates="2026-12-19,2026-12-26", nights_min=None, nights_max=None)
    assert swa.trip_pairs(w, TODAY) == [("2026-12-18", "2026-12-19"), ("2026-12-18", "2026-12-26"),
                                        ("2026-12-19", "2026-12-19"), ("2026-12-19", "2026-12-26")]


def test_oneway_has_no_return_leg():
    w = rt(trip_type="oneway")
    assert swa.return_dates(w, TODAY) == [] and [leg for leg, _ in swa.search_legs(w, TODAY)] == ["out", "out"]


def test_cheapest_round_trip_is_the_cheapest_flight_each_way():
    w = rt(dates="2026-12-18", nights_min=2, nights_max=2)
    legs = {
        "out": {"2026-12-18": [flight("A", 150, 12000), flight("B", 99, 9000), flight("C", 60, 4000, stops=2)]},
        "ret": {"2026-12-20": [flight("D", 120, 7000), flight("E", 180, 9000)]},
    }
    (t,) = swa.roundtrips(w, legs)
    assert t["out_cash"]["key"] == "B"  # C is cheaper but has 2 stops
    assert (t["cash"], t["points"], t["taxes"]) == (99 + 120, 9000 + 7000, 11.2)
    assert t["matched"] and t["nights"] == 2


def test_two_one_ways_can_mix_cash_and_points():
    w = rt(dates="2026-12-18", nights_min=2, nights_max=2, min_cpp=1.5)
    legs = {
        "out": {"2026-12-18": [flight("A", 100, 10000)]},   # 0.94 ¢/pt -> pay cash
        "ret": {"2026-12-20": [flight("B", 300, 15000)]},   # 1.96 ¢/pt -> pay points
    }
    (t,) = swa.roundtrips(w, legs)
    assert [m["pay"] for m in t["mix"]] == ["cash", "points"] and t["use"] == "Cash out, points back"
    assert (t["mix_cash"], t["mix_points"]) == (100, 15000)
    (t2,) = swa.roundtrips({**w, "mix_payment": 0}, legs)
    assert t2["use"] in ("Cash", "Points")  # one payment for the whole trip


def test_return_leg_has_its_own_departure_window():
    w = rt(dates="2026-12-18", nights_min=2, nights_max=2, ret_depart_after="15:00")
    legs = {"out": {"2026-12-18": [flight("A", 100, 9000, depart="08:00")]},
            "ret": {"2026-12-20": [flight("B", 80, 6000, depart="09:00"), flight("C", 140, 9000, depart="17:30")]}}
    (t,) = swa.roundtrips(w, legs)
    assert t["ret_cash"]["key"] == "C"


def test_price_target_applies_to_the_total():
    w = rt(dates="2026-12-18", nights_min=2, nights_max=2, max_price=200, max_points=None)
    legs = {"out": {"2026-12-18": [flight("A", 120, 9000)]}, "ret": {"2026-12-20": [flight("B", 110, 9000)]}}
    assert not swa.roundtrips(w, legs)[0]["matched"]  # 230 > 200 even though each leg is under


def test_booking_url_round_trip():
    u = swa.booking_url("AUS", "BWI", "2026-12-18", 2, "2026-12-22")
    assert "tripType=roundtrip" in u and "returnDate=2026-12-22" in u


# ---------- worker ----------
def test_worker_searches_both_directions_and_alerts_on_round_trips(worker, sent):
    uid = make_user()
    wid = make_watch(uid, trip_type="roundtrip", dates=future(10), nights_min=3, nights_max=4, max_price=500,
                     max_points=40000)
    res = worker.run_user(uid)
    routes = {(c[0], c[1]) for c in FakeSearcher.calls}
    assert routes == {("AUS", "BWI"), ("BWI", "AUS")}
    assert res["attempts"] == 2 * (1 + 2) and res["hits"] == 2  # 2 date pairs, both match
    assert {f["leg"] for f in db.query("SELECT leg FROM fares WHERE watch_id=?", (wid,))} == {"out", "ret"}
    assert "round trip" in db.one("SELECT last_status FROM watches WHERE id=?", (wid,))[0]
    html = sent[0][2]
    assert "→" in html and "nights" in html and "$380" in html  # 189.98 + 189.98
    sent.clear()
    assert worker.run_user(uid)["hits"] == 0  # same prices: no repeat alert


# ---------- web ----------
def test_create_round_trip_watch_and_view_results(client, worker):
    token = admin_client(client)
    r = client.post("/watches", data=watch_form(token, trip_type="roundtrip", return_mode="nights",
                                                nights_min="2", nights_max="4", mix_payment="1",
                                                ret_depart_after="06:00"), follow_redirects=False)
    assert r.status_code == 303, r.text
    wid = int(r.headers["location"].split("/")[2].split("?")[0])
    w = db.one("SELECT * FROM watches WHERE id=?", (wid,))
    assert (w["trip_type"], w["nights_min"], w["nights_max"], w["mix_payment"], w["ret_depart_after"]) == \
        ("roundtrip", 2, 4, 1, "06:00")
    uid = db.one("SELECT id FROM users WHERE username='admin'")["id"]
    worker.run_user(uid)
    page = client.get(f"/watches/{wid}").text
    assert "Best round trips" in page and "Fares by direction" in page and "tripType=roundtrip" in page
    assert page.count("data-sortable") == 2                       # both tables sort by column
    assert 'data-sort="379.96"' in page                           # raw totals for numeric sorting
    assert "Round trip" in client.get("/").text


def test_round_trip_validation(client):
    token = admin_client(client)
    r = client.post("/watches", data=watch_form(token, trip_type="roundtrip", return_mode="nights",
                                                nights_min="5", nights_max="2"))
    assert r.status_code == 400 and "max ≥ min" in r.text
    r = client.post("/watches", data=watch_form(token, trip_type="roundtrip", return_mode="dates",
                                                ret_dates="2000-01-01"))
    assert r.status_code == 400 and "No return date" in r.text
    r = client.post("/watches", data=watch_form(token, trip_type="roundtrip", return_mode="dates", ret_dates="soon"))
    assert r.status_code == 400 and "YYYY-MM-DD" in r.text


def test_pay_with_shows_amounts():
    out = {"cash": 292.0, "points": 25000, "points_taxes": 5.6}
    back = {"cash": 120.0, "points": 4000, "points_taxes": 5.6}
    assert swa.pay_amount("cash", out) == "$292"
    assert swa.pay_amount("points", back) == "4,000 pts + $5.60"
    trip = {"mix": [{"pay": "cash", "flight": out}, {"pay": "points", "flight": back}],
            "cash": 412.0, "points": 29000, "taxes": 11.2}
    w = {"mix_payment": 1, "min_cpp": 1.4}
    assert swa.use_detail(trip, w) == "Cash out ($292), points back (4,000 pts + $5.60)"
    assert swa.use_amount(trip, w) == "$292 out · 4,000 pts + $5.60 back"
    same = {**trip, "mix": [{"pay": "points", "flight": out}, {"pay": "points", "flight": back}]}
    assert swa.use_detail(same, w) == "Points: 29,000 pts + $11.20"
    assert swa.use_amount(same, w) == "29,000 pts + $11.20"
    r = {"cash": 292.0, "points": 19000, "points_taxes": 5.6, "cpp": 1.5}
    assert swa.recommend_detail(r, w) == "Points: 19,000 pts + $5.60"
    assert swa.recommend_detail({**r, "cpp": 1.0}, w) == "Cash: $292"


def test_round_trips_pair_directions_searched_at_different_check_ins(worker, sent, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "rotate", True)
    uid = make_user()
    make_watch(uid, trip_type="roundtrip", dates=future(10), nights_min=3, nights_max=3, max_price=500,
               max_points=40000)
    with db.tx() as con:   # the morning check-in read the outbound date only
        con.execute("INSERT INTO fares(watch_id, ts, leg, date, flight_key, flights, depart, arrive, stops, "
                    "duration, cash, points, points_taxes, matched, origin, dest) "
                    "SELECT id, ?, 'out', ?, 'k', '1234', '08:15', '12:40', 0, 205, 189.98, 12000, 5.6, 1, 'AUS', 'BWI' "
                    "FROM watches", (db.now(), future(10)))
        con.execute("INSERT INTO searched(watch_id, leg, date, ts, cash, points, changed_at) "
                    "SELECT id, 'out', ?, ?, 189.98, 12000, ? FROM watches",
                    (future(10), db.now(), db.now()))
    res = worker.run_user(uid, one_visit=True)          # the evening one reads the return
    assert {(c[0], c[1]) for c in FakeSearcher.calls} == {("BWI", "AUS")}
    assert res["hits"] >= 1 and "nights" in sent[0][2]
