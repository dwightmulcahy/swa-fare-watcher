import smtplib

import pytest

from app import db, mailer
from app.config import settings


class FakeSMTP:
    log = []
    fail_ports = set()
    auth_fail = False

    def __init__(self, host, port, timeout=None):
        self.port = port
        if port in FakeSMTP.fail_ports:
            raise smtplib.SMTPServerDisconnected("Connection unexpectedly closed")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def ehlo(self):
        pass

    def starttls(self):
        FakeSMTP.log.append(("starttls", self.port))

    def login(self, user, pw):
        if FakeSMTP.auth_fail:
            raise smtplib.SMTPAuthenticationError(535, b"bad")
        FakeSMTP.log.append(("login", self.port, pw))

    def sendmail(self, frm, to, msg):
        FakeSMTP.log.append(("send", self.port, to[0]))


@pytest.fixture
def smtp(monkeypatch):
    db.init()
    FakeSMTP.log, FakeSMTP.fail_ports, FakeSMTP.auth_fail = [], set(), False
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(settings, "gmail_user", "me@gmail.com")
    monkeypatch.setattr(settings, "gmail_password", "abcd efgh ijkl mnop")
    return FakeSMTP


def test_sends_on_465_with_spaces_removed(smtp):
    assert mailer.send("a@b.c", "s", "<p>x</p>")
    assert ("login", 465, "abcdefghijklmnop") in smtp.log and ("send", 465, "a@b.c") in smtp.log


def test_falls_back_to_587_when_465_is_cut(smtp):
    smtp.fail_ports = {465}
    assert mailer.send("a@b.c", "s", "<p>x</p>")
    assert ("starttls", 587) in smtp.log and ("send", 587, "a@b.c") in smtp.log
    assert "port 587" in db.one("SELECT message FROM logs WHERE source='mail' ORDER BY id DESC")["message"]


def test_both_ports_fail_logs_both(smtp):
    smtp.fail_ports = {465, 587}
    assert not mailer.send("a@b.c", "s", "<p>x</p>")
    msg = db.one("SELECT message FROM logs WHERE source='mail' ORDER BY id DESC")["message"]
    assert "port 465" in msg and "port 587" in msg


def test_bad_app_password_is_explained(smtp):
    smtp.auth_fail = True
    assert not mailer.send("a@b.c", "s", "<p>x</p>")
    assert "app password" in db.one("SELECT message FROM logs WHERE source='mail' ORDER BY id DESC")["message"]


def test_alert_email_is_southwest_styled_cards():
    from app import mailer
    f = {"flights": "502/52", "depart": "6:00 AM", "arrive": "4:10 PM", "stops": 2, "via": "MCO HOU",
         "duration": 610, "cash": 292.0, "points": 19000, "points_taxes": 5.6, "cpp": 1.5}
    trip = {"nights": 3, "out_date": "2027-04-05", "ret_date": "2027-04-08", "out_cash": f, "ret_cash": f,
            "out_points": f, "ret_points": f, "cash": 584.0, "points": 38000, "taxes": 11.2, "cpp": 1.5}
    out = mailer.render_hits([
        {"watch": "Spring <trip>", "url": "https://x/o", "ret_url": "https://x/r", "trip": trip,
         "use": "Points: 38,000 pts + $11.20"},
        {"watch": "One", "url": "https://x/1", "date": "2027-04-05", "row": f, "use": "Cash: $292"},
    ])
    assert "#304CB2" in out and "#FFBF27" in out and "Book on Southwest" in out
    assert "2 new deals" in out and "Spring &lt;trip&gt;" in out
    assert "2 stops (MCO, HOU)" in out and "$584" in out and "38,000 pts + $11.20" in out
    assert "Pay with: <b>Cash: $292</b>" in out and "https://x/r" in out
