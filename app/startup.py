"""Start-up check: the settings that make or break the app, reported once at start in the log
(info when fine, warn when not, so SWA_EMAIL_WARNINGS=warnings emails the problems) and on the
admin Site health page."""
import os
import shutil
from pathlib import Path

from . import applog, buildinfo, db, envfile, mailer, swa
from .config import settings

last = []    # [(name, ok, message)] from the latest run; ok None = just information


def _check(name, fn):
    try:
        ok, msg = fn()
    except Exception as e:
        ok, msg = False, f"check failed: {e.__class__.__name__}: {e}"
    return name, ok, msg


def _version():
    b = buildinfo.load()
    return None, f"{b.get('version')}" + (f" ({b['commit']})" if b.get("commit") else "")


def _storage():
    d = settings.data_dir
    dbf = d / "app.db"
    if not os.access(d, os.W_OK) or (dbf.exists() and not os.access(dbf, os.W_OK)):
        return False, (f"{d} or app.db isn't writable by uid {os.getuid()}: nothing can be saved "
                       "(chown the data folder to this uid)")
    free = shutil.disk_usage(d).free / 2**30
    if free < 1:
        return False, f"only {free:.1f} GB free in {d}"
    return True, f"{d} writable, {free:.0f} GB free"


def _secret():
    if os.environ.get("SECRET_KEY"):
        return True, "SECRET_KEY set"
    f = settings.data_dir / "secret.key"
    if f.exists():
        return True, f"{f} present; keep a copy somewhere safe (needed to restore a backup elsewhere)"
    return None, "secret.key will be created on first use"


def _gpu():
    why = swa.gpu_problem()
    if why:
        return False, why + ". Chrome draws in software, which is easier to tell from a real desktop"
    return True, (f"SWA_GPU on, {settings.gpu_device} readable and writable (whether Chrome really draws "
                  "with it is checked on the first search of the day)")


def _display():
    if settings.headless:
        return False, "SWA_HEADLESS=true: headless Chrome is more likely to be blocked"
    if not shutil.which("Xvfb"):
        return False, "Xvfb isn't installed: Chrome can't open its virtual screen"
    return True, "headed Chrome on a virtual screen (Xvfb)"


def _browser():
    chrome = any(Path(p).exists() for p in ("/opt/google/chrome/chrome", "/usr/bin/google-chrome",
                                            "/usr/bin/google-chrome-stable"))
    want = settings.browser_channel
    if want in ("auto", "chrome") and not chrome:
        return (False if want == "chrome" else None), \
            "Google Chrome not installed; Playwright's Chromium is used (a less common browser)"
    return True, f"SWA_BROWSER_CHANNEL={want}" + (", Google Chrome installed" if chrome else "")


def _humanize():
    if not settings.humanize:
        return False, "SWA_HUMANIZE=false: no human-like mouse, typing and reading"
    return True, "human-like input on"


def _schedule():
    if not settings.scheduler:
        return False, "SWA_SCHEDULER=false: watches only run on Search now"
    return True, "scheduled runs on" + (" (habitual check-in times)" if settings.habit_slots else "")


def _email(login=True):
    if not (settings.gmail_user and settings.gmail_password):
        return False, "GMAIL_USER / GMAIL_APP_PASSWORD not set: no alerts or seat emails can be sent"
    if not login:
        return True, f"sending as {settings.gmail_user}"
    return mailer.check_login()


def _links():
    url = settings.base_url
    if "localhost" in url or "127.0.0.1" in url:
        return False, f"BASE_URL is {url}: links in emails won't open from other devices"
    if url.startswith("https://") and not settings.cookie_secure:
        return False, "BASE_URL is https but COOKIE_SECURE=false: sign-in cookies can travel unencrypted"
    if url.startswith("http://") and settings.cookie_secure:
        return False, "COOKIE_SECURE=true on a plain-http BASE_URL: the browser drops the cookie, sign-in fails"
    return True, url


def _admin_password():
    n = db.one("SELECT COUNT(*) n FROM users WHERE is_admin=1 AND must_change_pw=1")["n"]
    if n:
        return False, f"{n} admin account(s) still on a temporary password"
    return True, "admin passwords set"


def _settings_file():
    p = envfile.path()
    if envfile.writable(p):
        return True, f"{p} writable (admin Settings page can save)"
    return None, f"{p} not writable: the admin Settings page is read-only (mount the .env read-write)"


def _backups():
    if settings.backup_keep == 0:
        return False, "SWA_BACKUP_KEEP=0: no daily database backups"
    msg = f"{settings.backup_keep} daily backups kept"
    if settings.backup_copy_dir:
        d = Path(settings.backup_copy_dir)
        if not d.is_dir():
            return False, f"{msg}, but SWA_BACKUP_COPY_DIR {d} doesn't exist (not mounted?)"
        if not os.access(d, os.W_OK):
            return False, f"{msg}, but SWA_BACKUP_COPY_DIR {d} isn't writable by uid {os.getuid()}"
        return True, f"{msg}, copied to {d}"
    return None, msg + "; no off-NAS copy (SWA_BACKUP_COPY_DIR)"


def _alerts():
    return None, f"SWA_EMAIL_WARNINGS={settings.email_warnings}" + (
        " (start-up problems below are emailed to admins)" if settings.email_warnings != "off" else "")


CHECKS = [("Version", _version), ("Storage", _storage), ("Secret key", _secret), ("GPU", _gpu),
          ("Display", _display), ("Browser", _browser), ("Human input", _humanize), ("Schedule", _schedule),
          ("Email", _email), ("Links", _links), ("Admin password", _admin_password),
          ("Settings file", _settings_file), ("Backups", _backups), ("Admin alerts", _alerts)]


def run(log=True):
    """Run every check; log each and a summary. Returns [(name, ok, message)]."""
    global last
    last = [_check(n, f) for n, f in CHECKS]
    if log:
        for name, ok, msg in last:
            applog.log(f"{name}: {msg}", "warn" if ok is False else "info", source="startup")
        bad = [n for n, ok, _ in last if ok is False]
        applog.log("Start-up check: " + (f"{len(bad)} problem(s): {', '.join(bad)}" if bad else "all good"),
                   "warn" if bad else "info", source="startup")
    return last
