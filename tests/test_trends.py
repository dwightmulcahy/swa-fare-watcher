import datetime as dt

from app import db, trends
from tests.test_worker import make_user, make_watch


def add(wid, date, key, ts_day, cash, depart="08:00"):
    with db.tx() as con:
        con.execute("INSERT INTO fares(watch_id, ts, date, leg, flight_key, flights, depart, arrive, stops, duration,"
                    " cash, origin, dest) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (wid, f"{ts_day}T10:00:00", date, "out", key, key, depart, "10:00", 0, 120, cash, "AUS", "DAL"))


def test_flight_trends_low_week_move_and_outlook():
    db.init()
    wid = make_watch(make_user())
    today = dt.date(2027, 3, 1)
    d = lambda n: (today + dt.timedelta(days=n)).isoformat()   # noqa: E731
    for i, cash in enumerate([150, 120, 140, 160]):          # upcoming flight, seen 4 days, ends at day 0
        add(wid, d(20), "WN1", d(-9 + 3 * i), cash)
    add(wid, d(25), "WN9", d(-5), 99)                        # not in the latest search: left out
    for n in range(12):                                      # departed flights: rose $30 from ~3 weeks out
        add(wid, d(-30 - n), f"P{n}", d(-50 - n), 100)
        add(wid, d(-30 - n), f"P{n}", d(-31 - n), 130)
    rows = trends.flight_trends(wid, today.isoformat())
    assert [r["flights"] for r in rows] == ["WN1"]
    r = rows[0]
    assert (r["cash"], r["low"], r["high"], r["above_low"], r["seen"]) == (160, 120, 160, 40, 4)
    assert r["week"] == 10 and r["spark"].count(",") == 4                   # vs 150 a week or more before
    assert r["outlook"] and "rose $30" in r["outlook"] and "2-4 weeks out" in r["outlook"]
