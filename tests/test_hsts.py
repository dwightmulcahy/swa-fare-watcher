from app.config import settings


def test_hsts_only_when_cookies_are_secure(client, monkeypatch):
    assert "Strict-Transport-Security" not in client.get("/login").headers
    monkeypatch.setattr(settings, "cookie_secure", True)
    assert client.get("/login").headers["Strict-Transport-Security"] == "max-age=31536000"
