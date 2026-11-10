import pytest
import threading
import time

from app import swa, db
from app.security import decrypt_json, encrypt_json
from tests.conftest import FakeSearcher, csrf, login
from tests.test_web import ADMIN_PW, activate, admin_client, create_user
from tests.test_worker import make_user, make_watch

CREDS = {"username": "dwight123", "password": "S3cret!pass"}


def with_creds(**kw):
    uid = make_user(swa_creds=encrypt_json(CREDS), auto_login=1, **kw)
    return uid


def user(uid):
    return db.one("SELECT * FROM users WHERE id=?", (uid,))


# ---------- worker ----------
def test_no_signin_without_saved_details(worker, sent):
    uid = make_user()
    make_watch(uid)
    assert "signin" not in worker.run_user(uid)
    assert FakeSearcher.signin_calls == []


def test_valid_session_needs_no_attempt(worker, sent):
    uid = with_creds()
    make_watch(uid)
    assert worker.run_user(uid)["signin"] == "already_signed_in"
    u = user(uid)
    assert u["auto_login_attempts"] == 0 and "still valid" in u["auto_login_status"]
    assert FakeSearcher.signin_calls[0]["password"] == "S3cret!pass"


def test_signs_in_when_expired_and_saves_session(worker, sent):
    uid = with_creds()
    make_watch(uid)
    FakeSearcher.signin = ["success"]
    res = worker.run_user(uid)
    assert res["signin"] == "success" and res["failures"] == 0
    u = user(uid)
    assert u["auto_login_attempts"] == 1 and u["auto_login_failures"] == 0
    assert decrypt_json(u["swa_state"])["cookies"] == [{"name": "fresh"}]  # saved even with no prior upload
    assert u["swa_status"] == "OK" and u["auto_login_status"].startswith("Signed in automatically")


def test_one_automatic_attempt_per_day(worker, sent):
    uid = with_creds()
    make_watch(uid)
    FakeSearcher.signin = ["success", "success"]
    worker.run_user(uid)
    assert worker.run_user(uid)["signin"] == "skipped"
    assert FakeSearcher.signin_calls[1]["allowed"] is False
    assert "limit reached" in user(uid)["auto_login_status"]


def test_manual_checks_allow_three_per_day(worker, sent):
    uid = with_creds()
    FakeSearcher.signin = ["timeout"] * 4
    worker.run_user(uid, signin_only=True)
    with db.tx() as con:  # keep it on despite failures so we test the daily cap alone
        con.execute("UPDATE users SET auto_login_failures=0 WHERE id=?", (uid,))
    worker.run_user(uid, signin_only=True)
    with db.tx() as con:
        con.execute("UPDATE users SET auto_login_failures=0, auto_login=1 WHERE id=?", (uid,))
    worker.run_user(uid, signin_only=True)
    with db.tx() as con:
        con.execute("UPDATE users SET auto_login_failures=0, auto_login=1 WHERE id=?", (uid,))
    assert worker.run_user(uid, signin_only=True)["signin"] == "skipped"
    assert FakeSearcher.calls == []  # sign-in-only runs never search


def test_wrong_password_turns_it_off_and_emails(worker, sent):
    uid = with_creds()
    make_watch(uid)
    FakeSearcher.signin = ["bad_credentials"]
    worker.run_user(uid)
    u = user(uid)
    assert u["auto_login"] == 0 and "rejected" in u["auto_login_status"]
    assert any("turned off" in subj for _, subj, _ in sent)


def test_two_failures_in_a_row_turn_it_off(worker, sent):
    uid = with_creds()
    FakeSearcher.signin = ["blocked", "blocked"]
    with pytest.raises(swa.Blocked):          # a block page stops the run (and pauses the schedule)
        worker.run_user(uid, signin_only=True)
    assert user(uid)["auto_login"] == 1 and user(uid)["auto_login_failures"] == 1
    assert user(uid)["blocked_until"]
    with pytest.raises(swa.Blocked):
        worker.run_user(uid, signin_only=True)
    u = user(uid)
    assert u["auto_login"] == 0 and "2 failed attempts" in u["auto_login_status"]


def test_verification_code_entered_in_app(worker, sent):
    uid = with_creds()
    FakeSearcher.signin = ["mfa"]

    def enter_code():
        for _ in range(100):
            req = db.one("SELECT * FROM code_requests WHERE user_id=? AND status='waiting'", (uid,))
            if req:
                with db.tx() as con:
                    con.execute("UPDATE code_requests SET code='123456', status='submitted' WHERE id=?", (req["id"],))
                return
            time.sleep(0.02)

    t = threading.Thread(target=enter_code)
    t.start()
    res = worker.run_user(uid, signin_only=True)
    t.join()
    assert res["signin"] == "success"
    req = db.one("SELECT * FROM code_requests WHERE user_id=?", (uid,))
    assert req["status"] == "used" and req["code"] is None  # code not kept
    assert any(subj == "Southwest verification code needed" for _, subj, _ in sent)


def test_verification_code_not_entered(worker, sent):
    uid = with_creds()
    FakeSearcher.signin = ["mfa"]
    res = worker.run_user(uid, signin_only=True)
    assert res["signin"] == "mfa_required"
    assert db.one("SELECT status FROM code_requests WHERE user_id=?", (uid,))["status"] == "expired"
    assert user(uid)["auto_login"] == 1  # not a failure: try again tomorrow


# ---------- web ----------
def test_save_details_encrypted_and_keep_password(client):
    token = admin_client(client)
    client.post("/settings/auto-login", data={"csrf": token, "swa_username": "dwight123",
                                              "swa_password": "S3cret!pass", "auto_login": "1"})
    u = db.one("SELECT * FROM users WHERE username='admin'")
    assert b"S3cret" not in bytes(u["swa_creds"]) and u["auto_login"] == 1
    assert decrypt_json(u["swa_creds"]) == CREDS
    page = client.get("/settings").text
    assert "dwight123" in page and "S3cret" not in page
    # blank password keeps the saved one; unticking turns it off
    client.post("/settings/auto-login", data={"csrf": token, "swa_username": "dwight123", "swa_password": ""})
    u = db.one("SELECT * FROM users WHERE username='admin'")
    assert decrypt_json(u["swa_creds"])["password"] == "S3cret!pass" and u["auto_login"] == 0
    # changing the username requires the password again
    r = client.post("/settings/auto-login", data={"csrf": token, "swa_username": "other", "swa_password": ""},
                    follow_redirects=False)
    assert "password" in r.headers["location"]
    client.post("/settings/auto-login/remove", data={"csrf": token})
    assert db.one("SELECT swa_creds FROM users WHERE username='admin'")["swa_creds"] is None


def test_try_now_queues_signin_only(client, worker):
    token = admin_client(client)
    r = client.post("/settings/auto-login/test", data={"csrf": token}, follow_redirects=False)
    assert "first" in r.headers["location"]
    client.post("/settings/auto-login", data={"csrf": token, "swa_username": "d", "swa_password": "p",
                                              "auto_login": "1"})
    client.post("/settings/auto-login/test", data={"csrf": token})
    uid = db.one("SELECT id FROM users WHERE username='admin'")["id"]
    assert uid in worker.signin_only and worker.pending[uid] == set()
    worker.enqueue(uid, [1])  # a search request turns it into a normal run
    assert uid not in worker.signin_only


def test_verify_page_and_ownership(client):
    token = admin_client(client)
    uid = db.one("SELECT id FROM users WHERE username='admin'")["id"]
    future = "2999-01-01T00:00:00"
    with db.tx() as con:
        rid = con.execute("INSERT INTO code_requests(user_id, created_at, expires_at) VALUES (?,?,?)",
                          (uid, db.now(), future)).lastrowid
    assert "Enter the code" in client.get("/").text  # banner on every page
    r = client.post(f"/verify/{rid}", data={"csrf": token, "code": "12 34 56"}, follow_redirects=False)
    assert "Code+sent" in r.headers["location"]
    assert db.one("SELECT code, status FROM code_requests WHERE id=?", (rid,))[:] == ("123456", "submitted")

    # someone else can't answer it
    with db.tx() as con:
        rid2 = con.execute("INSERT INTO code_requests(user_id, created_at, expires_at) VALUES (?,?,?)",
                           (uid, db.now(), future)).lastrowid
    temp = create_user(client, token, "mallory")
    t2 = activate(client, "mallory", temp)
    r = client.post(f"/verify/{rid2}", data={"csrf": t2, "code": "999999"}, follow_redirects=False)
    assert "expired" in r.headers["location"]
    assert db.one("SELECT status FROM code_requests WHERE id=?", (rid2,))["status"] == "waiting"
    client.cookies.clear()
    assert login(client, "admin", ADMIN_PW).status_code == 303
    assert csrf(client.get("/verify").text)
