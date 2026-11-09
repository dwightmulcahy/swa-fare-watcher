"""End-to-end sign-in against a local fake airline site, in a real browser with the app's
launch settings and human input. Skipped where no browser is installed (e.g. the CI pytest job);
the container smoke test covers the browser itself."""
import functools
import http.server
import os
import shutil
import threading
import time
from pathlib import Path

import pytest

from app.swa_login import Recorder, login

SITE = Path(__file__).parent / "fake_swa"


def _browser_available():
    if os.environ.get("SWA_BROWSER_TESTS") == "0":
        return False
    if os.uname().sysname == "Linux" and not os.environ.get("DISPLAY") and not shutil.which("Xvfb"):
        return False
    if shutil.which("google-chrome") or shutil.which("google-chrome-stable"):
        return True
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            return Path(p.chromium.executable_path).exists()
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _browser_available(), reason="no browser installed")


@pytest.fixture(scope="module")
def site():
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(SITE))
    handler.log_message = lambda *a: None
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/index.html"
    srv.shutdown()


def run(site, tmp_path, mode, username="dwight123", password="S3cret!pass", code="123456"):
    from app.swa import BrowserSearcher

    with BrowserSearcher(profile_key=f"t-{mode}") as b:
        b.human.speed = 0.1
        rec = Recorder(b.page, tmp_path / mode, log=lambda m: None)
        res = login(b.page, b.human, username, password, rec, code_provider=lambda: code,
                    home_url=f"{site}?mode={mode}")
        events = b.page.evaluate("window.events || null")
        events = {**(events or {}), "rememberMe": b.page.evaluate("window.rememberMe")}
    return res, events, rec


def test_signs_in_with_human_input(site, tmp_path):
    res, events, rec = run(site, tmp_path, "ok")
    assert res.status == "success" and res.name == "Dwight", res
    assert res.detail == "header shows account instead of Log in"  # no "Log out"/"Hi," on the page
    assert events.get("trap") is None, "followed the 'Forgot login?' link"
    assert events["untrusted"] == 0 and events["keys"] >= len("dwight123S3cret!pass")
    # only scrolls back up as far as it read down (the old bug scrolled up at the top of the page)
    assert events["wheelsUp"] <= events["wheelsDown"] + 2, events
    shots = sorted(p.name for p in rec.dir.glob("*.png"))
    assert shots[0].endswith("home.png") and shots[-1].endswith("signed-in.png")


def test_second_run_reuses_profile(site, tmp_path):
    from app.swa import BrowserSearcher

    run(site, tmp_path, "ok")
    with BrowserSearcher(profile_key="t-ok") as b:
        b.human.speed = 0.1
        res = login(b.page, b.human, "x", "y", Recorder(b.page, tmp_path / "again", log=lambda m: None),
                    home_url=f"{site}?mode=ok")
    assert res.status == "already_signed_in"


def test_verification_code_flow(site, tmp_path):
    res, events, _ = run(site, tmp_path, "mfa")
    assert res.status == "success", res
    assert events["rememberMe"] is True


def test_verification_code_unavailable(site, tmp_path):
    res, _, _ = run(site, tmp_path, "mfa", code=None)
    assert res.status == "mfa_required"


def test_wrong_password(site, tmp_path):
    res, _, _ = run(site, tmp_path, "ok", password="wrong-password")
    assert res.status == "bad_credentials" and "invalid" in res.detail


def test_blocked(site, tmp_path):
    res, _, _ = run(site, tmp_path, "block")
    assert res.status == "blocked"


# ---------- the app's automatic sign-in, end to end (worker + real browser + fake site) ----------
def _user_with_creds(password="S3cret!pass"):
    from app import db
    from app.security import encrypt_json, hash_password

    with db.tx() as con:
        return con.execute(
            "INSERT INTO users(username, email, pw_hash, must_change_pw, created_at, swa_creds, auto_login)"
            " VALUES ('u', '', ?, 0, ?, ?, 1)",
            (hash_password("x" * 10), db.now(), encrypt_json({"username": "dwight123", "password": password}))
        ).lastrowid


def _worker(site, mode, monkeypatch):
    from app.config import settings
    from app.worker import Worker

    monkeypatch.setattr(settings, "home_url", f"{site}?mode={mode}")
    w = Worker(sleep=lambda s: None)
    return w


def test_worker_signs_in_and_reuses_session(site, monkeypatch, sent):
    from app import db
    from app.security import decrypt_json

    uid = _user_with_creds()
    w = _worker(site, "ok", monkeypatch)
    assert w.run_user(uid, signin_only=True)["signin"] == "success"
    u = db.one("SELECT * FROM users WHERE id=?", (uid,))
    assert u["auto_login_status"].startswith("Signed in automatically")
    assert decrypt_json(u["swa_state"])["cookies"] is not None
    # next run: the persistent profile is still signed in, so no attempt is used
    assert w.run_user(uid, signin_only=True)["signin"] == "already_signed_in"
    assert db.one("SELECT auto_login_attempts FROM users WHERE id=?", (uid,))[0] == 1


def test_worker_waits_for_code_from_the_app(site, monkeypatch, sent):
    from app import db
    from app.config import settings

    monkeypatch.setattr(settings, "code_wait_seconds", 60)
    uid = _user_with_creds()
    w = _worker(site, "mfa", monkeypatch)

    def user_enters_code():
        for _ in range(600):
            try:
                req = db.one("SELECT * FROM code_requests WHERE user_id=? AND status='waiting'", (uid,))
            except Exception:
                return  # test already over
            if req:
                with db.tx() as con:
                    con.execute("UPDATE code_requests SET code='123456', status='submitted' WHERE id=?", (req["id"],))
                return
            time.sleep(0.1)

    t = threading.Thread(target=user_enters_code, daemon=True)
    t.start()
    res = w.run_user(uid, signin_only=True)
    t.join()
    assert res["signin"] == "success"
    assert any(subj == "Southwest verification code needed" for _, subj, _ in sent)


def test_worker_wrong_password_turns_off(site, monkeypatch, sent):
    from app import db

    uid = _user_with_creds(password="wrong-password")
    w = _worker(site, "ok", monkeypatch)
    assert w.run_user(uid, signin_only=True)["signin"] == "bad_credentials"
    assert db.one("SELECT auto_login FROM users WHERE id=?", (uid,))[0] == 0
    # the failed sign-in is logged with a real screenshot of the page
    from app import applog
    row = db.one("SELECT * FROM logs WHERE source='signin' AND message LIKE 'Sign-in result%'")
    shot = applog.attachment_path(row["attachment"])
    assert shot is not None and shot.read_bytes()[:4] == b"\x89PNG"


def test_calendar_skips_manifest_and_reads_fares(site, monkeypatch, tmp_path):
    from app.config import settings
    from app.swa import BrowserSearcher

    monkeypatch.setattr(settings, "calendar_url", site.replace("index.html", "calendar.html"))
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "debug_dir", tmp_path / "debug")
    with BrowserSearcher(profile_key="t-cal", human=False) as b:
        b._warmed = True
        days = b.calendar("LIR", "HOU", "2026-11-09", 1, "USD")
        assert days == {"2026-11-09": 481.0, "2026-11-10": 256.0, "2026-11-11": None}
        assert b.calendar_switches_currency
        points = b.calendar("LIR", "HOU", "2026-11-09", 1, "POINTS")      # a click, not a page load
        fetches = b.page.evaluate("window.calendarFetches")
    assert points == {"2026-11-09": 38000.0, "2026-11-10": 19500.0, "2026-11-11": None}
    assert fetches == ["USD", "POINTS"]                                 # same page: both on its log
    saved = sorted(p.read_text() for p in (tmp_path / "debug" / "calendar").glob("*.json"))
    assert any("calendar-data-USD.json" in t for t in saved) and any("calendar-data-POINTS" in t for t in saved)
    assert not any("someone@example.com" in t or "userinfo" in t for t in saved)   # account data never saved




def test_day_without_flights_is_empty_not_a_failure(site, monkeypatch, tmp_path):
    import json

    from app import swa
    from app.config import settings

    monkeypatch.setattr(swa, "SEARCH_URL", site.replace("index.html", "select-depart.html"))
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "debug_dir", tmp_path / "debug")
    with swa.BrowserSearcher(profile_key="t-noflights", human=False) as b:
        b._warmed = True
        b.page.route("**/air-booking/page/air/booking/shopping", lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({"success": False, "notifications": {"formErrors": [{"code": "ERROR__NO_FLIGHTS"}]}})))
        assert b.search("SJO", "JAX", "2026-12-16", 1, "USD") == {}
        assert "noflights=1" in b.page.url                       # Southwest's redirect happened
    assert not (tmp_path / "debug").exists() or not list((tmp_path / "debug").glob("*.json"))   # nothing to debug
