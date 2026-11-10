import datetime as dt
import json

import pytest

from app import swa
from tests.conftest import detail, payload

RULE = {"max_stops": 1, "max_duration_min": 360, "max_price": 250, "max_points": 15000,
        "min_cpp": 1.3, "depart_after": "06:00", "depart_before": "21:00"}


def row(**kw):
    c, p = kw.pop("cash", "189.98"), kw.pop("points", "12,000")
    return swa.combine(swa.parse_flights(payload(detail(c, "20.00", **kw))),
                       swa.parse_flights(payload(detail(p, "5.60", **kw))))[0]


def test_parse_picks_lowest_available_fare():
    d = detail("189.98", "20.00")
    d["fareProducts"]["ADULT"]["BUS"] = {"availabilityStatus": "AVAILABLE",
                                        "fare": {"totalFare": {"value": "400"}}}
    d["fareProducts"]["ADULT"]["SALE"] = {"availabilityStatus": "UNAVAILABLE",
                                         "fare": {"totalFare": {"value": "49"}}}
    (f,) = swa.parse_flights(payload(d)).values()
    assert f["fare"] == (189.98, 20.0)
    assert (f["stops"], f["duration"], f["depart"]) == (0, 205, "08:15")


def test_parse_unknown_shape_returns_none():
    assert swa.parse_flights({"unexpected": True}) is None


def test_cents_per_point_and_recommendation():
    r = row()
    assert r["cpp"] == pytest.approx(1.54)
    assert swa.passes(r, RULE)
    assert swa.recommend(r, RULE) == "Points"
    assert swa.recommend({**r, "cpp": 1.0}, RULE) == "Cash"


@pytest.mark.parametrize("change", [{"stops": 2}, {"duration": 400}, {"depart": "05:30"}, {"depart": "22:00"}])
def test_filters_reject(change):
    assert not swa.passes({**row(), **change}, RULE)


def test_passes_on_points_when_cash_too_high():
    r = {**row(), "cash": 400.0, "cpp": 1.5}
    assert swa.passes(r, RULE)
    assert not swa.passes({**r, "cpp": 1.0}, RULE)


def test_unset_limits_mean_any():
    r = {**row(), "stops": 3}
    assert swa.passes(r, {**RULE, "max_stops": None, "max_price": None})


def w(**kw):
    base = {"origin": "AUS", "destination": "BWI", "dates": None, "days_from": None, "days_to": None, "weekdays": None}
    return {**base, **kw}


def test_watch_dates_window_and_weekdays():
    today = dt.date(2026, 11, 6)  # Friday
    ds = swa.watch_dates(w(days_from=0, days_to=13, weekdays="fri"), today)
    assert ds == ["2026-11-06", "2026-11-13"]
    assert swa.searches_per_run(w(days_from=0, days_to=13, weekdays="fri"), today) == 4


def test_watch_dates_drops_past_and_caps():
    today = dt.date(2026, 11, 6)
    assert swa.watch_dates(w(dates="2026-01-01,2026-12-01"), today) == ["2026-12-01"]
    assert len(swa.watch_dates(w(days_from=0, days_to=200), today)) == 31


def test_normalize_cookie_editor_export():
    raw = json.dumps([
        {"domain": ".southwest.com", "name": "sid", "value": "abc", "path": "/", "expirationDate": 1893456000.5,
         "secure": True, "httpOnly": True, "sameSite": "no_restriction"},
        {"domain": ".southwest.com", "name": "tmp", "value": "x", "session": True, "sameSite": "unspecified"},
        {"domain": ".example.com", "name": "other", "value": "y"},
    ])
    st = swa.normalize_state(raw)
    assert [c["name"] for c in st["cookies"]] == ["sid", "tmp"]
    assert st["cookies"][0]["sameSite"] == "None" and st["cookies"][0]["expires"] == 1893456000.5
    assert st["cookies"][1]["expires"] == -1


def test_normalize_playwright_state():
    raw = json.dumps({"cookies": [{"domain": "www.southwest.com", "name": "a", "value": "b", "path": "/",
                                   "expires": -1, "httpOnly": False, "secure": True, "sameSite": "Lax"}],
                      "origins": [{"origin": "https://www.southwest.com", "localStorage": []},
                                  {"origin": "https://evil.example", "localStorage": []}]})
    st = swa.normalize_state(raw)
    assert len(st["cookies"]) == 1 and len(st["origins"]) == 1


def test_normalize_rejects_files_without_southwest_cookies():
    with pytest.raises(ValueError, match="No southwest.com cookies"):
        swa.normalize_state(json.dumps([{"domain": ".google.com", "name": "a", "value": "b"}]))


def test_flight_numbers_as_list_and_iso_times():
    # what Southwest actually sends: a list of flight numbers for connections
    d = detail("189.98", "20.00")
    d["flightNumbers"] = ["1234", "567"]
    d["departureTime"] = "2026-12-18T06:05:00"
    d["arrivalTime"] = "9:40"
    (f,) = swa.parse_flights(payload(d)).values()
    assert f["flights"] == "1234/567" and (f["depart"], f["arrive"]) == ("06:05", "09:40")
    r = swa.combine(swa.parse_flights(payload(d)), swa.parse_flights(payload(d)))[0]
    assert all(not isinstance(v, (list, dict)) for v in r.values())  # everything storable in SQLite


def test_flight_numbers_other_shapes():
    assert swa._text([{"number": "12"}, {"number": 34}]) == "12/34"
    assert swa._text(4321) == "4321" and swa._text(None) == ""


def test_booking_links_carry_the_full_search_query():
    from urllib.parse import parse_qs, urlsplit
    q = {k: v[0] for k, v in parse_qs(urlsplit(swa.booking_url("SJO", "HOU", "2026-11-09", 2)).query,
                                         keep_blank_values=True).items()}
    assert q == {k: str(v) for k, v in swa.search_query("SJO", "HOU", "2026-11-09", 2).items()}
    for k in ("adultsCount", "passengerType", "fareType", "departureTimeOfDay", "returnDate", "promoCode"):
        assert k in q
    assert (q["tripType"], q["returnDate"], q["adultsCount"]) == ("oneway", "", "2")
    rt = parse_qs(urlsplit(swa.booking_url("SJO", "HOU", "2026-11-09", 1, "2026-11-12")).query)
    assert rt["tripType"] == ["roundtrip"] and rt["returnDate"] == ["2026-11-12"]


def test_parse_real_southwest_shopping_response():
    from pathlib import Path
    data = json.loads((Path(__file__).parent / "fixtures" / "shopping-SJO-JAX-2027-04-05-USD.json").read_text())
    flights = {f["flights"]: f for f in swa.parse_flights(data).values()}
    assert len(flights) == 6
    f = flights["502/52/4972"]                    # SJO-MCO, MCO-HOU, HOU-JAX: two connections
    assert (f["stops"], f["duration"], f["depart"], f["arrive"]) == (2, 780, "08:45", "23:45")
    assert f["fare"] == (441.07, 123.07)          # cheapest available class (Wanna Get Away), per adult
    assert flights["1203/4972"]["stops"] == 1
    assert flights["502/4422/961"]["fare"][0] == 632.87      # WGA unavailable -> next cheapest (Plus)
    assert flights["1203/174/976"]["arrive"] == "10:20 +1"    # lands the next day
    rule = {**{k: None for k in ("max_stops", "max_duration_min", "max_price", "max_points",
                                 "depart_after", "depart_before")}, "min_cpp": 0, "max_stops": 1}
    row = lambda k: {**flights[k], "cash": flights[k]["fare"][0], "points": None, "cpp": None}
    assert swa.passes_leg(row("1203/4972"), rule, "out") and not swa.passes_leg(row("502/52/4972"), rule, "out")


def test_stop_airports_from_real_response():
    from pathlib import Path
    data = json.loads((Path(__file__).parent / "fixtures" / "shopping-SJO-JAX-2027-04-05-USD.json").read_text())
    flights = {f["flights"]: f for f in swa.parse_flights(data).values()}
    assert flights["502/52/4972"]["via"] == "MCO HOU"
    assert flights["1203/4972"]["via"] == "HOU"
    assert swa.stops_label(2, "MCO HOU") == "2 stops (MCO, HOU)"
    assert swa.stops_label(1, "HOU") == "1 stop (HOU)" and swa.stops_label(0, "") == "nonstop"


def test_stop_within_one_flight_counts_and_is_named():
    d = {"segments": [{"destinationAirportCode": "JAX", "numberOfStops": 1,
                       "stopsDetails": [{"destinationAirportCode": "BNA"}, {"destinationAirportCode": "JAX"}]}]}
    assert swa._stops(d) == 1 and swa._via(d) == "BNA"
