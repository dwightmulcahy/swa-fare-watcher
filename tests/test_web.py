import re

from app import db
from tests.conftest import csrf, future, login

ADMIN_PW = "admin-password-123"


def admin_client(client):
    r = login(client, "admin", ADMIN_PW)
    assert r.status_code == 303 and r.headers["location"] == "/"
    return csrf(client.get("/").text)


def create_user(client, token, username="alice", **limits):
    data = {"csrf": token, "username": username, "email": f"{username}@example.com",
            "max_watches": "2", "max_searches_per_day": "10", "expires_at": "", **limits}
    r = client.post("/admin/users", data=data)
    assert r.status_code == 200, r.text
    return re.search(r'<p class="secret" id="pw">([^<]+)</p>', r.text).group(1)


def activate(client, username, temp):
    client.cookies.clear()
    r = login(client, username, temp)
    assert r.status_code == 303
    page = client.get("/", follow_redirects=False)
    assert page.headers["location"] == "/password"
    token = csrf(client.get("/password").text)
    r = client.post("/password", data={"csrf": token, "current": temp, "new": "a-new-password-1",
                                       "confirm": "a-new-password-1"}, follow_redirects=False)
    assert r.status_code == 303
    return csrf(client.get("/").text)


def watch_form(token, **kw):
    return {"csrf": token, "origin": "aus", "destination": "BWI", "date_mode": "dates",
            "dates": f"{future(10)}, {future(11)}", "adults": "1", "max_stops": "1",
            "max_price": "250", "max_points": "", "min_cpp": "1.3", "active": "1", **kw}


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_requires_login_and_bad_password(client):
    assert client.get("/", follow_redirects=False).headers["location"] == "/login"
    assert login(client, "admin", "nope").status_code == 401


def test_security_headers(client):
    r = client.get("/login")
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]


def test_admin_creates_user_who_must_change_password(client):
    token = admin_client(client)
    temp = create_user(client, token)
    activate(client, "alice", temp)
    assert login(client, "alice", "a-new-password-1").status_code == 303


def test_user_watch_crud_and_isolation(client):
    token = admin_client(client)
    temp_a = create_user(client, token, "alice")
    temp_b = create_user(client, token, "bob")
    t = activate(client, "alice", temp_a)
    r = client.post("/watches", data=watch_form(t), follow_redirects=False)
    assert r.status_code == 303
    wid = int(r.headers["location"].split("/")[2].split("?")[0])
    w = db.one("SELECT * FROM watches WHERE id=?", (wid,))
    assert (w["origin"], w["name"], w["max_points"]) == ("AUS", "AUS→BWI", None)
    assert "AUS → BWI" in client.get("/").text

    r = client.post(f"/watches/{wid}", data=watch_form(t, name="Holidays", active="0"), follow_redirects=False)
    assert db.one("SELECT name, active FROM watches WHERE id=?", (wid,))[:] == ("Holidays", 0)

    # bob can't see or touch alice's watch
    tb = activate(client, "bob", temp_b)
    assert "not+found" in client.get(f"/watches/{wid}", follow_redirects=False).headers["location"]
    client.post(f"/watches/{wid}/delete", data={"csrf": tb})
    assert db.one("SELECT 1 FROM watches WHERE id=?", (wid,))


def test_validation_errors(client):
    token = admin_client(client)
    r = client.post("/watches", data=watch_form(token, origin="AUSTIN", max_price="", dates="12/18"))
    assert r.status_code == 400
    assert "3-letter airport code" in r.text and "YYYY-MM-DD" in r.text
    assert "max cash price" not in r.text  # price targets are optional now


def test_limits_enforced(client):
    token = admin_client(client)
    temp = create_user(client, token, "carol", max_watches="1", max_searches_per_day="6")
    t = activate(client, "carol", temp)
    # 4 dates = 8 searches > 6
    many = ", ".join(future(d) for d in range(10, 14))
    r = client.post("/watches", data=watch_form(t, dates=many))
    assert r.status_code == 400 and "over your limit of 6" in r.text
    assert client.post("/watches", data=watch_form(t), follow_redirects=False).status_code == 303
    r = client.post("/watches", data=watch_form(t, destination="LAS"))
    assert r.status_code == 400 and "at most 1 active watch" in r.text
    # paused watches don't count
    assert client.post("/watches", data=watch_form(t, destination="LAS", active="0"),
                       follow_redirects=False).status_code == 303


def test_run_now_queues_search(client, worker):
    token = admin_client(client)
    r = client.post("/watches", data=watch_form(token), follow_redirects=False)
    wid = int(r.headers["location"].split("/")[2].split("?")[0])
    client.post(f"/watches/{wid}/run", data={"csrf": token})
    uid = db.one("SELECT id FROM users WHERE username='admin'")["id"]
    assert worker.pending == {uid: {wid}}
    worker.run_user(uid, worker.pending.pop(uid))
    page = client.get(f"/watches/{wid}").text
    assert 'class="match"' in page and "$190" in page and "12,000" in page


def test_revoke_signs_user_out_and_blocks_login(client):
    token = admin_client(client)
    temp = create_user(client, token, "dave")
    activate(client, "dave", temp)
    dave_cookie = client.cookies.get("swa_session")
    uid = db.one("SELECT id FROM users WHERE username='dave'")["id"]

    client.cookies.clear()
    token = admin_client(client)
    client.post(f"/admin/users/{uid}/revoke", data={"csrf": token})
    assert db.one("SELECT active FROM users WHERE id=?", (uid,))["active"] == 0

    client.cookies.clear()
    client.cookies.set("swa_session", dave_cookie)
    assert client.get("/", follow_redirects=False).headers["location"] == "/login"
    assert login(client, "dave", "a-new-password-1").status_code == 403

    client.cookies.clear()
    token = admin_client(client)
    client.post(f"/admin/users/{uid}/restore", data={"csrf": token})
    client.cookies.clear()
    assert login(client, "dave", "a-new-password-1").status_code == 303


def test_expired_user_cannot_sign_in(client):
    token = admin_client(client)
    temp = create_user(client, token, "erin", expires_at="2000-01-01")
    client.cookies.clear()
    r = login(client, "erin", temp)
    assert r.status_code == 403 and "expired" in r.text


def test_admin_edits_limits_and_resets_password(client):
    token = admin_client(client)
    create_user(client, token, "frank")
    uid = db.one("SELECT id FROM users WHERE username='frank'")["id"]
    client.post(f"/admin/users/{uid}", data={"csrf": token, "email": "f@example.com", "max_watches": "",
                                             "max_searches_per_day": "100", "expires_at": "2030-01-01"})
    u = db.one("SELECT * FROM users WHERE id=?", (uid,))
    assert (u["max_watches"], u["max_searches_per_day"], u["expires_at"]) == (None, 100, "2030-01-01")
    r = client.post(f"/admin/users/{uid}/reset-password", data={"csrf": token})
    new_temp = re.search(r'id="pw">([^<]+)<', r.text).group(1)
    client.cookies.clear()
    assert login(client, "frank", new_temp).status_code == 303


def test_admin_cannot_revoke_or_demote_self(client):
    token = admin_client(client)
    uid = db.one("SELECT id FROM users WHERE username='admin'")["id"]
    client.post(f"/admin/users/{uid}/revoke", data={"csrf": token})
    assert db.one("SELECT active FROM users WHERE id=?", (uid,))["active"] == 1
    r = client.post(f"/admin/users/{uid}", data={"csrf": token, "email": ""})
    assert r.status_code == 400 and "own admin role" in r.text


def test_non_admin_blocked_from_admin(client):
    token = admin_client(client)
    temp = create_user(client, token, "gina")
    activate(client, "gina", temp)
    assert "Admins+only" in client.get("/admin", follow_redirects=False).headers["location"]
    r = client.post("/admin/users", data={"csrf": csrf(client.get("/").text), "username": "evil"},
                    follow_redirects=False)
    assert r.status_code == 303 and not db.one("SELECT 1 FROM users WHERE username='evil'")


def test_csrf_required(client):
    admin_client(client)
    r = client.post("/watches", data=watch_form("wrong"), follow_redirects=False)
    assert "expired" in r.headers["location"]
    assert db.one("SELECT COUNT(*) n FROM watches")["n"] == 0


def test_swa_session_upload_is_encrypted(client):
    token = admin_client(client)
    cookie_json = '[{"domain": ".southwest.com", "name": "sid", "value": "SECRETVALUE"}]'
    r = client.post("/settings/swa", data={"csrf": token, "state_text": cookie_json}, follow_redirects=False)
    assert "saved" in r.headers["location"]
    blob = db.one("SELECT swa_state FROM users WHERE username='admin'")["swa_state"]
    assert b"SECRETVALUE" not in bytes(blob)
    r = client.post("/settings/swa", data={"csrf": token, "state_text": "not json"}, follow_redirects=False)
    assert "valid+JSON" in r.headers["location"]


def test_login_rate_limited(client):
    for _ in range(10):
        login(client, "admin", "wrong")
    assert login(client, "admin", ADMIN_PW).status_code == 429


def test_dashboard_has_delete(client):
    token = admin_client(client)
    client.post("/watches", data=watch_form(token), follow_redirects=False)
    wid = db.one("SELECT id FROM watches ORDER BY id DESC")["id"]
    page = client.get("/").text
    assert f'action="/watches/{wid}/delete"' in page and "data-confirm" in page
    assert client.post(f"/watches/{wid}/delete", data={"csrf": token}, follow_redirects=False).status_code == 303
    assert db.one("SELECT id FROM watches WHERE id=?", (wid,)) is None


def test_too_many_specific_dates_rejected(client):
    token = admin_client(client)
    many = ", ".join(future(n) for n in range(5, 5 + 40))
    r = client.post("/watches", data=watch_form(token, dates=many), follow_redirects=False)
    assert r.status_code == 400 and "At most 31 departure dates (40 picked)" in r.text
    page = client.get("/watches/new").text
    assert page.count('data-max="31"') == 2      # both date pickers know the limit


def test_block_pause_banner_and_resume(client):
    token = admin_client(client)
    with db.tx() as con:
        con.execute("UPDATE users SET blocked_until='2999-01-01T00:00', blocked_reason='calendar: HTTP 403'")
    page = client.get("/").text
    assert "Scheduled runs are paused until 2999-01-01 00:00" in page and "calendar: HTTP 403" in page
    assert client.post("/resume", data={"csrf": token}, follow_redirects=False).status_code == 303
    assert "Scheduled runs are paused" not in client.get("/").text
