import datetime as dt
import json

from app import checkin, db, seats
from tests.test_worker import make_user, make_watch


def test_boarding_positions_are_read_not_gates():
    assert checkin.boarding("Gate C12 · Terminal A\nPAT LEE Boarding Group 3 Seat 14A") == (["Group 3"], ["14A"])
    assert checkin.boarding("Boarding position: A23 · Gate B4") == (["A23"], [])
    assert "confirmationNumber=ABC123" in checkin.checkin_url("ABC123", "Pat", "Lee")


def _watch(conf="ABC123"):
    db.init()
    uid = make_user()
    wid = make_watch(uid)
    day = (dt.date.today() + dt.timedelta(days=5)).isoformat()
    with db.tx() as con:
        con.execute("UPDATE watches SET kind='rebook', checkin_auto=1, booking_conf=?, booking_first='Pat', "
                    "booking_last='Lee', booked=? WHERE id=?",
                    (conf, json.dumps([{"leg": "out", "date": day, "time": "13:15", "from": "AUS"}]), wid))
    return uid, wid, day


def test_checkin_job_planned_24h_before_departure():
    uid, wid, day = _watch()
    made = db.sync_jobs(wid)
    assert made == [("out", seats.opens_at(day, "13:15", "AUS", hours=24))]
    with db.tx() as con:
        con.execute("UPDATE watches SET checkin_auto=0 WHERE id=?", (wid,))
    db.sync_jobs(wid)
    assert db.query("SELECT * FROM checkin_jobs WHERE watch_id=?", (wid,)) == []


def _run(worker, monkeypatch, result):
    class Browser:
        def __enter__(self): return self
        def __exit__(self, *a): pass

    class Fake:
        def __init__(self, *a, **k): pass
        def run(self, until, **k):
            if isinstance(result, Exception):
                raise result
            return result
    monkeypatch.setattr(checkin, "CheckIn", Fake)
    job = db.query("SELECT j.*, w.user_id FROM checkin_jobs j JOIN watches w ON w.id=j.watch_id")[0]
    return worker.run_checkin_job(dict(job), browser_factory=Browser)


def test_checkin_job_emails_the_boarding_group(worker, sent, monkeypatch):
    uid, wid, day = _watch()
    db.sync_jobs(wid)
    status, positions, note = _run(worker, monkeypatch, (["Group 3"], ["14A"]))
    assert status == "done" and "Group 3" in note
    assert "SWA checked in" in sent[-1][1] and "Group 3" in sent[-1][2] and "14A" in sent[-1][2]
    assert "check-in/index.html?confirmationNumber=ABC123" in sent[-1][2]
    assert worker.due_checkin_jobs(dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=9)) == []


def test_checkin_job_retries_then_asks_you(worker, sent, monkeypatch):
    uid, wid, day = _watch()
    db.sync_jobs(wid)
    for want in ("pending", "pending", "failed"):
        status, _, note = _run(worker, monkeypatch, RuntimeError("page changed"))
        assert status == want
        if want == "pending":
            assert not sent
            with db.tx() as con:
                con.execute("UPDATE checkin_jobs SET next_at=NULL")
    assert "3 times" in note and "check-in needs you" in sent[-1][1]


def test_rebook_form_turns_on_checkin(client):
    from tests.test_web import admin_client
    token = admin_client(client)
    day = (dt.date.today() + dt.timedelta(days=9)).isoformat()
    form = {"csrf": token, "kind": "rebook", "origin": "AUS", "destination": "LIR", "trip_type": "oneway",
            "adults": "1", "out_date": day, "out_cash": "300", "checkin_auto": "1", "active": "1"}
    r = client.post("/watches", data=form)
    assert r.status_code == 400 and "departure time" in r.text and "confirmation number" in r.text
    r = client.post("/watches", data={**form, "out_time": "13:15", "booking_conf": "abc123",
                                      "booking_first": "Pat", "booking_last": "Lee"}, follow_redirects=False)
    wid = int(r.headers["location"].split("/")[2].split("?")[0])
    assert tuple(db.one("SELECT checkin_auto, booking_conf FROM watches WHERE id=?", (wid,))) == (1, "ABC123")
    page = client.get(f"/watches/{wid}").text
    assert "Automatic check-in" in page and "check-in opens" in page
