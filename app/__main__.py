"""CLI.

  python -m app serve [--host 0.0.0.0] [--port 8080]   run web app + worker + daily scheduler
  python -m app selftest                               launch the browser as searches do and exit (CI smoke)
  python -m app login-test [options]                   try an automated Southwest sign-in with your
                                                       credentials, using the app's browser + human input
  python -m app swa-login [storage_state.json]         sign in to Southwest yourself; save the session
                                                       file to upload in Settings
"""
import argparse
import datetime as dt
import getpass
import os
import sys
from pathlib import Path


def load_dotenv(path=".env"):
    """Minimal .env reader: KEY=VALUE lines; real environment variables win."""
    p = Path(path)
    if not p.is_file():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        os.environ.setdefault(k.strip(), v)


def _visible_display():
    return not sys.platform.startswith("linux") or bool(os.environ.get("DISPLAY"))


def swa_login(out):
    from .swa import BrowserSearcher, HOME_URL

    if not _visible_display():
        sys.exit("swa-login needs a desktop with a visible browser.")
    with BrowserSearcher(profile_key="manual-login", human=False) as b:
        b.page.goto(HOME_URL)
        input("Log in to Rapid Rewards in the browser window, then press Enter here... ")
    Path(out).write_text(__import__("json").dumps(b.state, indent=2))
    print(f"Saved {out}. Upload it under Settings → Southwest session.")


def login_test(a):
    """Standalone automated sign-in test. Exit code: 0 signed in, 2 needs verification code,
    3 wrong credentials, 4 blocked, 5 couldn't find the form / timed out."""
    if a.server:
        os.environ.pop("DISPLAY", None)  # behave like the container: private Xvfb display
    from .config import settings
    from .swa import BrowserSearcher, wipe_profile
    from .swa_login import Recorder, login, watch_network

    username = a.username or os.environ.get("SWA_USERNAME") or input("Rapid Rewards number or username: ").strip()
    password = os.environ.get("SWA_PASSWORD") or getpass.getpass("Southwest password: ")
    if not username or not password:
        sys.exit("Need SWA_USERNAME and SWA_PASSWORD (in .env or the environment).")

    interactive = sys.stdin.isatty() and not a.no_prompt
    watching = interactive and _visible_display() and not settings.headless

    def code_provider():
        if not interactive:
            return None
        return input("Enter the verification code Southwest sent you (blank to stop): ").strip() or None

    def manual_assist(msg):
        if not watching:
            return False
        input(msg + " ")
        return True

    profile = a.profile
    if a.fresh:
        wipe_profile(profile)
    out_dir = settings.data_dir / "login-test" / dt.datetime.now().strftime("%Y%m%d-%H%M%S")

    print(f"Profile: {settings.data_dir / 'profiles' / profile}  (persists between runs; --fresh to reset)")
    print(f"Screenshots and logs: {out_dir}")
    print(f"Mode: {'visible window' if _visible_display() else 'hidden virtual display (server mode)'}"
          f"{'' if settings.humanize else ', humanize OFF'}")

    result, state = None, None
    with BrowserSearcher(profile_key=profile) as b:
        rec = Recorder(b.page, out_dir)
        rec.log(f"Browser: {b.channel}  "
                f"UA: {b.page.evaluate('navigator.userAgent')}  "
                f"webdriver: {b.page.evaluate('navigator.webdriver')}")
        watch_network(b.page, rec)
        try:
            result = login(b.page, b.human, username, password, rec, code_provider=code_provider,
                           home_url=a.home_url, manual_assist=manual_assist if not a.no_assist else None)
        except Exception as e:
            from .swa_login import LoginResult
            rec.shot("exception")
            result = LoginResult("error", f"{e.__class__.__name__}: {str(e).splitlines()[0][:200]}")
            rec.log(f"Error: {result.detail}")
        if a.keep_open and watching:
            input("Browser left open. Press Enter to close... ")
    state = b.state

    print()
    print(f"RESULT: {result.status.upper()}  {result.detail}")
    if result.name:
        print(f"Signed in as: {result.name}")
    if result.ok and state:
        Path(a.save_state).write_text(__import__("json").dumps(state, indent=2))
        os.chmod(a.save_state, 0o600)
        print(f"Session saved to {a.save_state}: upload it in the app under Settings → Southwest session.")
    hints = {
        "mfa_required": "Southwest asked for a verification code. Run again in a terminal to type it in; "
                        "the saved profile should make later sign-ins skip it.",
        "bad_credentials": "Check SWA_USERNAME / SWA_PASSWORD. Don't retry repeatedly: the account can lock.",
        "blocked": "Southwest's bot protection stopped it. Look at the screenshots and network.log.",
        "not_found": "The login form didn't match what the script looks for. Send me the screenshots.",
        "timeout": "No recognisable result. The last screenshot shows where it stopped.",
        "error": "Unexpected error; see steps.log and the last screenshot.",
    }
    if result.status in hints:
        print(hints[result.status])
    print(f"Details: {out_dir}")
    return result.exit_code


def test_email(to=None):
    """Connect to Gmail on each port and report exactly what happens, then send a test message."""
    import smtplib
    import socket

    from . import db, mailer
    from .config import settings
    if not (settings.gmail_user and settings.gmail_password):
        print("GMAIL_USER and GMAIL_APP_PASSWORD must both be set.")
        return 2
    print(f"GMAIL_USER={settings.gmail_user}  app password: {len(settings.gmail_password.replace(' ', ''))} "
          "characters (should be 16)")
    host = settings.gmail_host
    try:
        print(f"{host} resolves to: {', '.join(sorted({a[4][0] for a in socket.getaddrinfo(host, None)}))}")
    except OSError as e:
        print(f"DNS lookup for {host} failed: {e}")
    for port in (465, 587):
        try:
            conn = smtplib.SMTP_SSL(host, port, timeout=20) if port == 465 else smtplib.SMTP(host, port, timeout=20)
            with conn as c:
                if port == 587:
                    c.ehlo()
                    c.starttls()
                    c.ehlo()
                c.login(settings.gmail_user, settings.gmail_password.replace(" ", ""))
            print(f"port {port}: OK (connected and logged in)")
        except smtplib.SMTPAuthenticationError as e:
            print(f"port {port}: connected, but Gmail rejected the login ({e.smtp_code}): use an app password "
                  "from myaccount.google.com/apppasswords")
        except Exception as e:
            print(f"port {port}: FAILED {e!r}")
    db.init()
    ok = mailer.send(to or settings.gmail_user, "Fare Watcher test email",
                     "<p>This is a test from Fare Watcher. Email alerts are working.</p>")
    print("Test email sent." if ok else "Test email NOT sent (details in the Log tab).")
    return 0 if ok else 1


def main():
    load_dotenv()
    ap = argparse.ArgumentParser(prog="python -m app")
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("serve")
    s.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    s.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    sub.add_parser("selftest")
    lg = sub.add_parser("swa-login")
    lg.add_argument("out", nargs="?", default="storage_state.json")
    sub.add_parser("check-tz", help="check the browser time zone matches this machine's internet location")
    te = sub.add_parser("test-email", help="check Gmail sending on both ports and send a test message")
    te.add_argument("to", nargs="?", help="default: GMAIL_USER")
    lt = sub.add_parser("login-test", help="try an automated Southwest sign-in")
    lt.add_argument("--username", help="default: SWA_USERNAME")
    lt.add_argument("--profile", default="login-test", help="browser profile name under data/profiles/")
    lt.add_argument("--fresh", action="store_true", help="start from an empty profile (new 'device')")
    lt.add_argument("--server", action="store_true", help="run in a hidden virtual display like the container")
    lt.add_argument("--save-state", default="storage_state.json", help="where to save the session on success")
    lt.add_argument("--keep-open", action="store_true", help="leave the window open at the end")
    lt.add_argument("--no-assist", action="store_true", help="never pause for you to help in the window")
    lt.add_argument("--no-prompt", action="store_true", help="never ask for input (fail on verification code)")
    lt.add_argument("--home-url", default=None, help=argparse.SUPPRESS)  # tests point this at a fake site
    a = ap.parse_args()

    if a.cmd == "selftest":
        from .swa import selftest
        return selftest()
    if a.cmd == "check-tz":
        from .tzcheck import enforce
        sys.exit(0 if enforce() else 2)
    if a.cmd == "test-email":
        sys.exit(test_email(a.to))
    if a.cmd == "swa-login":
        return swa_login(a.out)
    if a.cmd == "login-test":
        from .tzcheck import enforce
        if a.home_url is None and not enforce():
            sys.exit(2)
        if a.home_url is None:
            from .swa_login import HOME_URL
            a.home_url = HOME_URL
        sys.exit(login_test(a))

    import time

    import uvicorn

    from .tzcheck import enforce
    from .web import create_app
    if not enforce(log=lambda m: print(m, file=sys.stderr, flush=True)):
        print("[tz-check] Not starting. Fix the time zone (see above) and restart.", file=sys.stderr, flush=True)
        time.sleep(60)  # with restart: unless-stopped, don't hammer the lookup services in a tight loop
        sys.exit(2)
    uvicorn.run(create_app(), host=getattr(a, "host", "0.0.0.0"), port=getattr(a, "port", 8080),
                proxy_headers=True, forwarded_allow_ips=os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1"),
                log_level=os.environ.get("LOG_LEVEL", "info"))


if __name__ == "__main__":
    main()
