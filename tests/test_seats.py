import datetime as dt
import json
from pathlib import Path

from app import seats

LABELS = (Path(__file__).parent / "fixtures" / "seatmap-737-800-labels.txt").read_text().splitlines()
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
