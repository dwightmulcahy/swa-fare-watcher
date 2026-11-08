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
