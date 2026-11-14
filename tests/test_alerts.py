from app import db
from app.worker import pick_alerts, record_alerted
from tests.conftest import FakeSearcher, future
from tests.test_worker import make_user, make_watch


def c(wid, cash, points, name="x"):
    return {"watch_id": wid, "row": {"cash": cash, "points": points}, "watch": name}


def test_first_search_emails_cheapest_then_only_better_deals():
    db.init()
    wid = make_watch(make_user())
    with db.tx() as con:
        first = pick_alerts(con, wid, [c(wid, 300, 25000), c(wid, 200, 18000), c(wid, 250, 15000)], limit=1)
        # cheapest cash, plus the cheapest-by-points one since it's a different fare
        assert [h["row"]["cash"] for h in first] == [200, 250]
        record_alerted(con, first)
    with db.tx() as con:
        assert pick_alerts(con, wid, [c(wid, 200, 18000), c(wid, 260, 16000)]) == []   # nothing better
        better = pick_alerts(con, wid, [c(wid, 199, 30000), c(wid, 400, 14000), c(wid, 205, 15000)])
        assert sorted(h["row"]["cash"] for h in better) == [199, 400]
        record_alerted(con, better)
        row = con.execute("SELECT cash, points FROM alerted WHERE watch_id=? AND key='__best__'", (wid,)).fetchone()
        assert (row["cash"], row["points"]) == (199, 14000)


def test_limit_caps_one_email():
    db.init()
    wid = make_watch(make_user())
    with db.tx() as con:
        out = pick_alerts(con, wid, [c(wid, 100 + i, None) for i in range(12)])
    assert [h["row"]["cash"] for h in out] == [100, 101, 102, 103, 104]


def test_worker_emails_once_until_cheaper(worker, sent):
    uid = make_user()
    make_watch(uid)
    assert worker.run_user(uid)["hits"] >= 1 and len(sent) == 1
    sent.clear()
    assert worker.run_user(uid)["hits"] == 0 and sent == []        # same fares: no email
    # a new date with the same fare isn't a better deal either
    with db.tx() as con:
        con.execute("UPDATE watches SET dates = dates || ?", ("," + future(15),))
    from tests.conftest import FakeSearcher
    FakeSearcher.calls = []
    assert worker.run_user(uid)["hits"] == 0 and sent == []
    assert any(call[2] == future(15) for call in FakeSearcher.calls)   # the new date was searched


def test_price_drop_and_history_line():
    from app import mailer
    from app.worker import price_drop
    assert price_drop({"cash": 250.0, "points": 12000}, None) is None
    assert price_drop({"cash": 250.0, "points": 12000}, {"cash": 298.0, "points": 15000}) == "$48 / 3,000 pts"
    assert price_drop({"cash": 300.0, "points": 12000}, {"cash": 298.0, "points": 15000}) == "3,000 pts"
    line = mailer._history({"drop": "$48", "low": (310.0, "2027-03-03T07:30:00")})
    assert "↓ $48</b> since your last alert" in line and "lowest seen before: $310 on Mar 3" in line
    assert mailer._history({}) == ""


def test_lowest_seen_ignores_this_run():
    from app.worker import lowest_seen
    uid = make_user()
    wid = make_watch(uid)
    d = future(10)
    with db.tx() as con:
        for ts, cash in (("2027-01-01T07:00:00", 320.0), ("2027-01-02T07:00:00", 299.0), ("2027-01-03T07:00:00", 250.0)):
            con.execute("INSERT INTO fares(watch_id, ts, date, flight_key, cash) VALUES (?,?,?,?,?)",
                        (wid, ts, d, "1|x", cash))
        assert lowest_seen(con, wid, d, "2027-01-03T06:00:00") == (299.0, "2027-01-02T07:00:00")
        assert lowest_seen(con, wid, d, "2027-01-01T06:00:00") is None


def test_digest_mode_saves_alerts_and_sends_once_a_day(worker, sent):
    import datetime as dt
    from app.worker import Worker
    uid = make_user(alert_mode="daily")
    make_watch(uid)
    out = worker.run_user(uid)
    assert out["hits"] and not sent                       # nothing emailed during the run
    queued = db.query("SELECT COUNT(*) n FROM digest_hits")[0]["n"]
    assert queued == out["hits"]
    assert worker.run_user(uid)["hits"] == 0              # already alerted: not queued again
    day = dt.datetime.now().replace(hour=17, minute=0)
    assert Worker.send_digests(now=day) == 0              # before SWA_DIGEST_AT
    assert Worker.send_digests(now=day.replace(hour=18, minute=5)) == 1
    assert "digest" in sent[0][1] and "this day" in sent[0][1] and "Book on Southwest" in sent[0][2]
    assert db.query("SELECT COUNT(*) n FROM digest_hits")[0]["n"] == 0
    with db.tx() as con:
        con.execute("INSERT INTO digest_hits(user_id, created, hit) VALUES (?,?,?)",
                    (uid, db.now(), '{"watch_id": 1, "watch": "x", "date": "2027-01-01", "row": {"cash": 1, "points": null, "points_taxes": null, "cpp": null, "flights": "1", "depart": "1", "arrive": "2", "stops": 0, "duration": 60}, "use": "Cash", "url": "u"}'))
    assert Worker.send_digests(now=day.replace(hour=20)) == 0          # same day
    with db.tx() as con:
        con.execute("UPDATE users SET alert_mode='instant' WHERE id=?", (uid,))
    assert Worker.send_digests(now=day.replace(hour=9)) == 1           # switched back: sent now


def test_settings_saves_alert_mode(client):
    from tests.test_web import admin_client
    t = admin_client(client)
    client.post("/settings", data={"csrf": t, "email": "a@b.co", "alert_mode": "weekly"})
    assert db.one("SELECT alert_mode FROM users WHERE username='admin'")["alert_mode"] == "weekly"
    assert "weekly digest" in client.get("/settings").text
    client.post("/settings", data={"csrf": t, "email": "a@b.co", "alert_mode": "bogus"})
    assert db.one("SELECT alert_mode FROM users WHERE username='admin'")["alert_mode"] == "instant"


def test_rebook_offer_same_flight_or_any():
    from app import swa
    rows = [{"flights": "502/52", "cash": 240.0, "points": 15000}, {"flights": "99", "cash": 199.0, "points": 12000}]
    o = swa.rebook_offer({"flights": "502 / 52", "cash": 280, "points": None}, rows)
    assert o["row"]["flights"] == "502/52" and o["save_cash"] == 40
    assert swa.rebook_offer({"flights": "", "cash": 280, "points": None}, rows)["row"]["flights"] == "99"
    assert swa.rebook_offer({"flights": "99", "cash": None, "points": 12000}, rows) is None   # not cheaper
    assert swa.rebook_offer({"flights": "", "cash": None, "points": 16000}, rows)["save_points"] == 4000


def test_rebook_alert_sent_once_and_skips_digest(worker, sent):
    import json
    uid = make_user(alert_mode="daily")
    d = future(30)     # not one of the watch's dates: searched only for the rebook check
    make_watch(uid, booked=json.dumps([{"leg": "out", "date": d, "flights": "99", "cash": 450, "points": None}]))
    worker.run_user(uid)
    assert len(sent) == 1 and "booked flight" in sent[0][1]
    assert "Rebook and save $30" in sent[0][2] and "Change flight" in sent[0][2]
    assert any(c[2] == d for c in FakeSearcher.calls)
    q = [json.loads(r["hit"]) for r in db.query("SELECT hit FROM digest_hits")]
    assert q and all(h["date"] != d for h in q)        # the booked date isn't one of the watch's deals
    worker.run_user(uid)
    assert len(sent) == 1                              # same price: no second rebook email


def test_watch_form_saves_booked_flight(client):
    import json
    from tests.test_web import admin_client, watch_form
    t = admin_client(client)
    data = {**watch_form(t), "booked_out_date": future(10), "booked_out_flights": "502/52", "booked_out_cash": "289"}
    loc = client.post("/watches", data=data, follow_redirects=False).headers["location"].split("?")[0]
    wid = int(loc.rstrip("/").split("/")[-1])
    b = json.loads(db.one("SELECT booked FROM watches WHERE id=?", (wid,))["booked"])
    assert b == [{"leg": "out", "date": future(10), "cash": 289, "points": None, "time": None, "from": "AUS",
                  "flights": "502/52"}]
    assert 'value="502/52"' in client.get(loc + "/edit").text
    bad = {**watch_form(t), "booked_out_date": future(10)}
    assert "what you paid" in client.post("/watches", data=bad).text
