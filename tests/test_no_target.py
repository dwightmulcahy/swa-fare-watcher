import datetime as dt

from app import db, swa
from tests.test_roundtrip import flight, rt
from tests.test_web import admin_client, watch_form
from tests.test_worker import make_user, make_watch


def test_form_allows_no_price_target(client):
    token = admin_client(client)
    r = client.post("/watches", data=watch_form(token, max_price="", max_points=""), follow_redirects=False)
    assert r.status_code == 303
    w = db.one("SELECT * FROM watches ORDER BY id DESC")
    assert w["max_price"] is None and w["max_points"] is None
    assert "Cheapest fare" in client.get("/").text


def test_rolling_window_from_picked_dates(client):
    token = admin_client(client)
    start = (dt.date.today() + dt.timedelta(days=10)).isoformat()
    end = (dt.date.today() + dt.timedelta(days=20)).isoformat()
    r = client.post("/watches", data=watch_form(token, date_mode="window", dates="",
                                                window_start=start, window_end=end), follow_redirects=False)
    assert r.status_code == 303
    w = db.one("SELECT * FROM watches ORDER BY id DESC")
    assert (w["days_from"], w["days_to"]) == (10, 20)
    page = client.get(f"/watches/{w['id']}/edit").text
    assert f'value="{start}"' in page and f'value="{end}"' in page
    r = client.post("/watches", data=watch_form(token, date_mode="window", dates="",
                                                window_start=end, window_end=start), follow_redirects=False)
    assert r.status_code == 400 and "end on or after the start" in r.text


def test_no_target_emails_only_the_cheapest_flight_and_only_when_it_drops(worker, sent):
    uid = make_user()
    make_watch(uid, max_price=None, max_points=None)
    res = worker.run_user(uid)
    assert res["hits"] == 1                       # 2 dates × 2 flights, one email row
    assert len(sent) == 1 and "$190" in sent[0][2]
    assert worker.run_user(uid)["hits"] == 0      # same price again: quiet


def test_no_target_round_trip_reports_the_cheapest_pair():
    w = rt(max_price=None, max_points=None)
    legs = {"out": {"2026-12-18": [flight("A", 150, 12000)], "2026-12-19": [flight("B", 90, 7000)]},
            "ret": {d: [flight("R" + d[-2:], 100, 8000)] for d in ("2026-12-20", "2026-12-21", "2026-12-22")}}
    trips = swa.roundtrips(w, legs)
    assert all(t["matched"] for t in trips) and trips[0]["out_date"] == "2026-12-19"


def test_no_target_calendar_keeps_cheapest_days():
    w = rt(trip_type="oneway", max_price=None, max_points=None)
    legs = [("out", d) for d in ("2026-12-18", "2026-12-19", "2026-12-20")]
    cal = {"out": {"2026-12-18": {"cash": 300, "points": None}, "2026-12-19": {"cash": 120, "points": None},
                   "2026-12-20": {"cash": 200, "points": None}}}
    assert swa.calendar_pick(w, cal, legs, 2) == [("out", "2026-12-19"), ("out", "2026-12-20")]
