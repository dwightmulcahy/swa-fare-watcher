import re
import time

from app import db
from app.security import recovery_hash, totp_code, totp_match, totp_secret
from tests.conftest import csrf, login
from tests.test_web import ADMIN_PW, admin_client

RFC = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"   # RFC 6238 test key "12345678901234567890"


def test_totp_matches_rfc_6238_and_refuses_replays():
    assert totp_code(RFC, 59 // 30) == "287082" and totp_code(RFC, 1111111109 // 30) == "081804"
    assert totp_match(RFC, "081 804", now=1111111109) == 1111111109 // 30
    assert totp_match(RFC, "081804", now=1111111109 + 30) is not None          # one step of drift is fine
    assert totp_match(RFC, "081804", now=1111111109 + 90) is None
    assert totp_match(RFC, "081804", now=1111111109, after=1111111109 // 30) is None   # already used
    assert totp_match(RFC, "abc", now=0) is None


def _now(secret):
    return totp_code(secret, int(time.time() // 30))


def _enable(client):
    t = admin_client(client)
    page = client.get("/settings/2fa").text
    assert 'src="data:image/svg+xml' in page and "otpauth://totp/" in page
    secret = re.search(r"secret=([A-Z2-7]+)", page).group(1)
    assert "That code didn" in client.post("/settings/2fa/enable", data={"csrf": t, "code": "000000"}).text \
        or _now(secret) == "000000"
    r = client.post("/settings/2fa/enable", data={"csrf": t, "code": _now(secret)})
    codes = re.findall(r"\b[0-9a-f]{6}-[0-9a-f]{6}\b", r.text)
    assert len(codes) == 8
    u = db.one("SELECT * FROM users WHERE username='admin'")
    assert u["totp_secret"] and secret.encode() not in bytes(u["totp_secret"])   # stored encrypted
    assert recovery_hash(codes[0]) in u["totp_recovery"] and codes[0] not in u["totp_recovery"]
    return secret, codes


def test_sign_in_needs_the_code_after_the_password(client):
    secret, codes = _enable(client)
    client.cookies.clear()
    r = login(client, "admin", ADMIN_PW)
    assert r.status_code == 200 and "Enter your code" in r.text and "swa_session" not in client.cookies
    assert client.get("/", follow_redirects=False).headers["location"] == "/login"
    assert client.post("/login/code", data={"code": "12345"}).status_code == 401
    with db.tx() as con:   # the enable step was used up; allow its code again
        con.execute("UPDATE users SET totp_step=-1")
    r = client.post("/login/code", data={"code": _now(secret)}, follow_redirects=False)
    assert r.status_code == 303 and "swa_session" in client.cookies
    # a recovery code works once
    client.cookies.clear()
    login(client, "admin", ADMIN_PW)
    assert client.post("/login/code", data={"code": codes[0].upper()}, follow_redirects=False).status_code == 303
    client.cookies.clear()
    login(client, "admin", ADMIN_PW)
    assert client.post("/login/code", data={"code": codes[0]}).status_code == 401


def test_three_wrong_codes_go_back_to_the_password(client):
    _enable(client)
    client.cookies.clear()
    login(client, "admin", ADMIN_PW)
    for _ in range(2):
        assert client.post("/login/code", data={"code": "000001"}).status_code == 401
    r = client.post("/login/code", data={"code": "000001"}, follow_redirects=False)
    assert r.status_code == 303 and "Sign+in+again" in r.headers["location"]
    assert client.post("/login/code", data={"code": "000001"}, follow_redirects=False).status_code == 303


def test_code_step_without_a_password_is_refused(client):
    r = client.post("/login/code", data={"code": "123456"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")


def test_turning_off_needs_password_and_code(client):
    secret, codes = _enable(client)
    t = csrf(client.get("/settings").text)
    r = client.post("/settings/2fa/disable", data={"csrf": t, "password": "nope", "code": codes[1]},
                    follow_redirects=False)
    assert "Wrong" in r.headers["location"]
    client.post("/settings/2fa/disable", data={"csrf": t, "password": ADMIN_PW, "code": codes[1]})
    assert not db.one("SELECT totp_secret FROM users WHERE username='admin'")["totp_secret"]


def test_admin_page_nudges_admins_without_two_step(client):
    admin_client(client)
    assert "no two-step sign-in" in client.get("/admin").text
    assert totp_secret() != totp_secret()


def test_reset_2fa_from_the_command_line(client, monkeypatch, capsys):
    import sys

    import pytest

    from app.__main__ import main
    _enable(client)
    monkeypatch.setattr(sys, "argv", ["app", "reset-2fa", "admin"])
    with pytest.raises(SystemExit) as e:
        main()
    assert e.value.code == 0 and not db.one("SELECT totp_secret FROM users WHERE username='admin'")["totp_secret"]


def test_reset_password_from_the_command_line(client, monkeypatch, capsys):
    import sys

    import pytest

    from app.__main__ import main
    from app.security import verify_password
    admin_client(client)
    for args, code in ((["reset-password", "nobody"], 1), (["reset-password", "admin"], 0)):
        monkeypatch.setattr(sys, "argv", ["app", *args])
        with pytest.raises(SystemExit) as e:
            main()
        assert e.value.code == code
    out = capsys.readouterr().out
    assert "no user 'nobody' (users: admin" in out
    pw = re.search(r"temporary password for admin: (\S+)", out).group(1)
    u = db.one("SELECT * FROM users WHERE username='admin'")
    assert verify_password(pw, u["pw_hash"]) and u["must_change_pw"] == 1
    assert not db.one("SELECT 1 FROM sessions WHERE user_id=?", (u["id"],))       # signed out
    assert pw not in str(db.query("SELECT message FROM logs"))                     # never logged
    assert client.get("/", follow_redirects=False).headers["location"] == "/login"
    assert login(client, "admin", pw).status_code == 303
    assert "/password" in client.get("/", follow_redirects=False).headers["location"]   # must choose a new one
