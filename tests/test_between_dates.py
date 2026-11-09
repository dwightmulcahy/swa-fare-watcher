import datetime as dt

from app import db, swa
from tests.conftest import future
from tests.test_web import admin_client, watch_form


def base(**kw):
    return {"origin": "AUS", "destination": "BWI", "dates": None, "days_from": None, "days_to": None,
            "weekdays": None, **kw}


def test_between_dates_fixed_range():
    today = dt.date(2026, 11, 6)  # Friday
    w = base(date_from="2026-11-04", date_to="2026-11-10")
    assert swa.watch_dates(w, today) == ["2026-11-06", "2026-11-07", "2026-11-08", "2026-11-09", "2026-11-10"]
    assert swa.watch_dates({**w, "weekdays": "sat,sun"}, today) == ["2026-11-07", "2026-11-08"]
    assert swa.watch_dates(w, dt.date(2026, 11, 11)) == []           # over: nothing left to search
    assert len(swa.watch_dates(base(date_from="2026-11-06", date_to="2027-03-01"), today)) == 31


def test_form_between_dates(client):
    token = admin_client(client)
    a, b = future(5), future(9)
    r = client.post("/watches", data=watch_form(token, date_mode="between", dates="",
                                                between_start=a, between_end=b), follow_redirects=False)
    assert r.status_code == 303, r.text
    w = db.one("SELECT * FROM watches ORDER BY id DESC")
    assert (w["date_from"], w["date_to"], w["dates"], w["days_to"]) == (a, b, None, None)
    edit = client.get(f"/watches/{w['id']}/edit").text
    assert 'value="between" checked' in edit and f'value="{a}"' in edit
    assert "Between" in client.get("/").text
    # switching back to specific dates clears the range
    client.post(f"/watches/{w['id']}", data=watch_form(token), follow_redirects=False)
    assert db.one("SELECT date_from FROM watches WHERE id=?", (w["id"],))["date_from"] is None


def test_form_between_dates_validation(client):
    token = admin_client(client)
    for start, end, msg in ((future(9), future(5), "on or after the first"),
                            ("2020-01-01", "2020-01-05", "already passed"),
                            ("", "", "first and last departure date")):
        r = client.post("/watches", data=watch_form(token, date_mode="between", dates="",
                                                    between_start=start, between_end=end))
        assert r.status_code == 400 and msg in r.text, msg
