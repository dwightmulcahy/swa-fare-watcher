from app import db, swa
from tests.conftest import FakeSearcher, future
from tests.test_roundtrip import rt
from tests.test_worker import make_user, make_watch


def test_parse_calendar_finds_daily_lows_in_nested_data():
    payload = {"data": {"searchResults": {"calendar": [
        {"month": "2026-11", "days": [
            {"date": "2026-11-09", "lowestPrice": {"value": "481"}, "originalPrice": "99"},
            {"date": "2026-11-10", "fare": {"amount": "1,234"}},
            {"date": "2026-11-11", "status": "UNAVAILABLE"},
            {"date": "2026-11-12T00:00:00", "price": 256, "taxes": 5.6},
        ]}]}}}
    assert swa.parse_calendar(payload) == {"2026-11-09": 481.0, "2026-11-10": 1234.0,
                                           "2026-11-11": None, "2026-11-12": 256.0}


def test_parse_calendar_without_prices_is_none():
    assert swa.parse_calendar({"dates": [{"date": "2026-11-09"}]}) is None
    assert swa.parse_calendar({"nothing": True}) is None


def test_calendar_months_open_on_first_wanted_day():
    assert swa.calendar_months(["2026-12-03", "2026-11-20", "2026-11-25"]) == \
        {"2026-11": "2026-11-20", "2026-12": "2026-12-03"}


def ow(**kw):
    return rt(trip_type="oneway", **kw)


def test_pick_oneway_keeps_days_under_target_cheapest_first():
    legs = [("out", "2026-12-18"), ("out", "2026-12-19"), ("out", "2026-12-20"), ("out", "2026-12-21")]
    cal = {"out": {"2026-12-18": {"cash": 320, "points": 30000},   # misses both targets
                   "2026-12-19": {"cash": 199, "points": 15000},
                   "2026-12-20": {"cash": None, "points": None},   # sold out
                   }}                                              # 21st not on calendar
    assert swa.calendar_pick(ow(), cal, legs, 6) == [("out", "2026-12-19"), ("out", "2026-12-21")]
    assert swa.calendar_pick(ow(), cal, legs, 1) == [("out", "2026-12-19")]
    assert swa.calendar_pick(ow(), {"out": None}, legs, 1) == legs   # unreadable: search everything


def test_pick_points_only_day_under_points_target():
    cal = {"out": {"2026-12-18": {"cash": 400, "points": 9000}}}
    assert swa.calendar_pick(ow(), cal, [("out", "2026-12-18")], 6) == [("out", "2026-12-18")]


def test_pick_roundtrip_by_pair_sum():
    w = rt()  # out 18/19, back 20-22, max $300 / 25k pts per person
    legs = swa.search_legs(w)
    cal = {"out": {"2026-12-18": {"cash": 120, "points": 9000}, "2026-12-19": {"cash": 250, "points": 20000}},
           "ret": {"2026-12-20": {"cash": 100, "points": 8000}, "2026-12-21": {"cash": 200, "points": 16000},
                   "2026-12-22": {"cash": 90, "points": 7000}}}
    # pairs: 18-20 $220, 18-21 $320/25k, 19-21 $450/36k, 19-22 $340/27k -> keep 18-20 and 18-21
    assert sorted(swa.calendar_pick(w, cal, legs, 6)) == [("out", "2026-12-18"), ("ret", "2026-12-20"),
                                                          ("ret", "2026-12-21")]
    assert sorted(swa.calendar_pick(w, cal, legs, 1)) == [("out", "2026-12-18"), ("ret", "2026-12-20")]
    # return calendar unreadable: every pair is a candidate
    assert len(swa.calendar_pick(w, {"out": cal["out"], "ret": None}, legs, 6)) == len(legs)


class CalendarSearcher(FakeSearcher):
    lows = {}   # fare_type -> {date: amount}
    cal_calls = []

    def calendar(self, origin, dest, date, adults, fare_type):
        CalendarSearcher.cal_calls.append((origin, dest, date[:7], fare_type))
        return CalendarSearcher.lows.get(fare_type)


def test_worker_reads_calendar_then_searches_only_good_dates(worker, sent):
    d1, d2 = future(10), future(11)
    CalendarSearcher.cal_calls = []
    CalendarSearcher.lows = {"USD": {d1: 900, d2: 190}, "POINTS": {d1: 90000, d2: 12000}}
    worker.searcher_factory = CalendarSearcher
    uid = make_user()
    wid = make_watch(uid)
    res = worker.run_user(uid)
    months = {d1[:7], d2[:7]}
    assert len(CalendarSearcher.cal_calls) == 2 * len(months)
    assert {c[2] for c in FakeSearcher.calls} == {d2}               # d1 skipped
    assert res["attempts"] == 2 and res["hits"] == 1
    assert db.searches_today(uid) == 2 + 2 * len(months)            # calendar loads count too
    cal = db.query("SELECT * FROM calendar_fares WHERE watch_id=?", (wid,))
    assert {(r["date"], r["cash"], r["points"]) for r in cal} == {(d1, 900, 90000), (d2, 190, 12000)}


def test_worker_nothing_under_target(worker, sent):
    d1, d2 = future(10), future(11)
    CalendarSearcher.lows = {"USD": {d1: 900, d2: 800}, "POINTS": {d1: 90000, d2: 80000}}
    worker.searcher_factory = CalendarSearcher
    uid = make_user()
    wid = make_watch(uid)
    worker.run_user(uid)
    assert not [c for c in FakeSearcher.calls if c[2] in (d1, d2)]
    assert "under your target" in db.one("SELECT last_status FROM watches WHERE id=?", (wid,))["last_status"]


def test_worker_falls_back_when_calendar_unreadable(worker, sent):
    CalendarSearcher.lows = {}
    worker.searcher_factory = CalendarSearcher
    uid = make_user()
    make_watch(uid)
    assert worker.run_user(uid)["attempts"] == 4


def test_best_calendar_ignores_build_manifest():
    manifest = {"artifact": "spa-air-low-fare-calendar-v2", "created": "Tuesday, 27 October 2026 at 09:46"}
    fares = {"days": [{"date": "2026-11-09", "lowestPrice": {"price": "481"}}]}
    url, _, days = swa.best_calendar([(".../low-fare-calendar/manifest.json", manifest), ("/api/cal", fares)])
    assert url == "/api/cal" and days == {"2026-11-09": 481.0}
    assert swa.best_calendar([("m", manifest)]) is None


def _fixture(name):
    import json
    from pathlib import Path
    return json.loads((Path(__file__).parent / "fixtures" / name).read_text())


def test_parse_real_southwest_calendar():
    days = swa.parse_calendar(_fixture("calendar-LIR-JAX-2027-03-USD.json"))
    assert len(days) == 31 and sum(v is not None for v in days.values()) == 22
    assert days["2027-03-01"] == 368.52                          # cheapest of WGA/PLU/ANY/BUS
    assert days["2027-03-02"] is None and days["2027-03-04"] == 312.52


def test_api_errors_are_reported():
    err = _fixture("calendar-points-currency-error.json")
    assert swa.parse_calendar(err) is None
    assert "VALIDATION__CURRENCY_CODE__REQUIRED" in swa._api_errors([("u", err)])
    assert swa._api_errors([("u", {"success": True})]) == ""
