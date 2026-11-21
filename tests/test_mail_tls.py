import ssl

from app import mailer
from app.config import settings


class FakeSMTP:
    made = []

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port, self.context, self.tls = host, port, context, None
        FakeSMTP.made.append(self)

    def __enter__(self): return self
    def __exit__(self, *a): pass
    def ehlo(self): pass
    def starttls(self, context=None): self.tls = context
    def login(self, user, pw): self.user = user
    def sendmail(self, *a): pass
    def close(self): pass


def _verifying(ctx):
    return isinstance(ctx, ssl.SSLContext) and ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname


def test_gmail_connections_check_the_certificate(monkeypatch):
    monkeypatch.setattr(settings, "gmail_user", "me@example.com")
    monkeypatch.setattr(settings, "gmail_password", "abcd efgh ijkl mnop")
    FakeSMTP.made.clear()
    monkeypatch.setattr(mailer.smtplib, "SMTP_SSL", FakeSMTP)
    monkeypatch.setattr(mailer.smtplib, "SMTP", FakeSMTP)
    with mailer._connect(465, 5) as s:
        assert _verifying(s.context)
    with mailer._connect(587, 5) as s:
        assert _verifying(s.tls)                                   # STARTTLS with a verifying context too
    assert mailer.check_login()[0]
    assert all(_verifying(c.context) or _verifying(c.tls) for c in FakeSMTP.made)


def test_a_failed_certificate_check_says_so():
    e = ssl.SSLCertVerificationError("bad")
    e.verify_message = "self-signed certificate"
    assert "intercepting" in mailer._why(e) and "self-signed" in mailer._why(e)
