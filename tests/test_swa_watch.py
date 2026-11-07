import datetime as dt
import sqlite3

import pytest

import swa_watch as w


def detail(fare, taxes, flights="1234", depart="08:15", stops=0, duration=205, status="AVAILABLE"):
    return {
        "flightNumbers": flights,
        "departureTime": depart,
        "arrivalTime": "12:40",
        "departureDateTime": f"2026-12-18T{depart}",
        "totalDuration": duration,
        "numberOfStops": stops,
        "fareProducts": {"ADULT": {
            "WGA": {"availabilityStatus": status,
                    "fare": {"totalFare": {"value": fare}, "totalTaxesAndFees": {"value": taxes}}},
            "BUS": {"availabilityStatus": "AVAILABLE",
                    "fare": {"totalFare": {"value": "99,999"}, "totalTaxesAndFees": {"value": "1"}}},
        }},
    }


def payload(*details):
    return {"data": {"searchResults": {"airProducts": [{"details": list(details)}]}}}


RULE = {"max_stops": 1, "max_duration_min": 360, "max_price": 250,
        "max_points": 15000, "min_cpp": 1.3, "depart_after": "06:00", "depart_before": "21:00"}


def row(**kw):
    c, p = kw.pop("cash", "189.98"), kw.pop("points", "12,000")
    cash = w.parse_flights(payload(detail(c, "20.00", **kw)))
    pts = w.parse_flights(payload(detail(p, "5.60", **kw)))
    return w.combine(cash, pts)[0]


def test_parse_picks_lowest_available_fare():
    flights = w.parse_flights(payload(detail("189.98", "20.00")))
    (f,) = flights.values()
    assert f["fare"] == (189.98, 20.0)
    assert (f["stops"], f["duration"], f["depart"]) == (0, 205, "08:15")


def test_parse_skips_sold_out_fare():
    (f,) = w.parse_flights(payload(detail("99.00", "9", status="UNAVAILABLE"))).values()
    assert f["fare"] == (99999.0, 1.0)


def test_parse_unknown_shape_returns_none():
    assert w.parse_flights({"unexpected": True}) is None


def test_cents_per_point_and_recommendation():
    r = row()
    assert r["cpp"] == pytest.approx(1.54)
    assert w.passes(r, RULE)
    assert w.recommend(r, RULE) == "Points"
    assert w.recommend({**r, "cpp": 1.0}, RULE) == "Cash"


@pytest.mark.parametrize("change", [
    {"stops": 2},
    {"duration": 400},
    {"depart": "05:30"},
    {"depart": "22:00"},
])
def test_filters_reject(change):
    assert not w.passes({**row(), **change}, RULE)


def test_passes_on_points_when_cash_too_high():
    r = {**row(), "cash": 400.0, "cpp": 1.5}
    assert w.passes(r, RULE)
    assert not w.passes({**r, "cpp": 1.0}, RULE)


def test_route_dates_weekday_filter():
    dates = w.route_dates({"days_ahead": [0, 13], "weekdays": ["fri"]})
    assert len(dates) == 2
    assert all(dt.date.fromisoformat(d).weekday() == 4 for d in dates)


def test_route_dates_drops_past_dates():
    assert w.route_dates({"dates": ["2000-01-01"]}) == []


def test_is_new_low(tmp_path, monkeypatch):
    con = sqlite3.connect(tmp_path / "f.db")
    con.execute("CREATE TABLE alerted(key TEXT PRIMARY KEY, cash REAL, points REAL)")
    r = {"cash": 100.0, "points": 8000.0}
    assert w.is_new_low(con, "k", r)
    con.execute("INSERT INTO alerted VALUES ('k', 100, 8000)")
    assert not w.is_new_low(con, "k", r)
    assert w.is_new_low(con, "k", {**r, "points": 7000.0})


def test_seconds_until_wraps_to_tomorrow():
    now = dt.datetime(2026, 11, 6, 8, 0)
    assert w.seconds_until("07:20", now) == pytest.approx(23 * 3600 + 20 * 60)
    assert w.seconds_until("09:00", now) == 3600


def test_render_email():
    html = w.render([{"row": row(), "url": "https://x", "route": "AUS→BWI",
                      "date": "2026-12-18", "use": "Points"}])
    assert "12,000 + $5.60" in html and "1.54" in html and "3h25" in html
