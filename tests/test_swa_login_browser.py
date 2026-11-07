"""End-to-end sign-in against a local fake airline site, in a real browser with the app's
launch settings and human input. Skipped where no browser is installed (e.g. the CI pytest job);
the container smoke test covers the browser itself."""
import functools
import http.server
import os
import shutil
import threading
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
