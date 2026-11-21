from app import db
from tests.conftest import login
from tests.test_web import ADMIN_PW, admin_client, create_user


def test_username_is_locked_after_5_wrong_passwords_others_still_sign_in(client):
    alice_pw = create_user(client, admin_client(client))
    client.cookies.clear()
    for _ in range(5):
        assert login(client, "admin", "wrong").status_code == 401
    assert login(client, "admin", ADMIN_PW).status_code == 429          # right password, still paused
    assert login(client, "alice", alice_pw).status_code == 303          # another account isn't
    assert "paused for 15 minutes" in db.one("SELECT message FROM logs ORDER BY id DESC")["message"]


def test_unknown_usernames_are_counted_but_not_logged(client):
    for _ in range(6):
        assert login(client, "nobody", "x").status_code in (401, 429)
    assert login(client, "nobody", "x").status_code == 429
    assert not db.one("SELECT 1 FROM logs WHERE message LIKE '%nobody%'")


def test_untrusted_proxy_is_reported_once(client):
    for _ in range(2):
        client.post("/login", data={"username": "admin", "password": "x"},
                    headers={"X-Forwarded-For": "203.0.113.9"})
    rows = db.query("SELECT message FROM logs WHERE message LIKE '%FORWARDED_ALLOW_IPS%'")
    assert len(rows) == 1
