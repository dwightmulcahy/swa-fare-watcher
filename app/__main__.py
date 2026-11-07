"""CLI.

  python -m app serve [--host 0.0.0.0] [--port 8080]   run web app + worker + daily scheduler
  python -m app selftest                               launch Chromium and exit (CI smoke)
  python -m app swa-login [storage_state.json]         desktop helper: sign in to Southwest,
                                                       save the session file to upload in Settings
"""
import argparse
import os
import sys


def swa_login(out):
    from playwright.sync_api import sync_playwright

    if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
        sys.exit("swa-login needs a desktop with a visible browser.")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        ctx = browser.new_context(viewport={"width": 1366, "height": 900}, locale="en-US")
        page = ctx.new_page()
        page.goto("https://www.southwest.com/")
        input("Log in to Rapid Rewards in the browser window, then press Enter here... ")
        ctx.storage_state(path=out)
        browser.close()
    print(f"Saved {out}. Upload it under Settings → Southwest session.")


def main():
    ap = argparse.ArgumentParser(prog="python -m app")
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("serve")
    s.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    s.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    sub.add_parser("selftest")
    lg = sub.add_parser("swa-login")
    lg.add_argument("out", nargs="?", default="storage_state.json")
    a = ap.parse_args()

    if a.cmd == "selftest":
        from .swa import selftest
        return selftest()
    if a.cmd == "swa-login":
        return swa_login(a.out)

    import uvicorn

    from .web import create_app
    uvicorn.run(create_app(), host=getattr(a, "host", "0.0.0.0"), port=getattr(a, "port", 8080),
                proxy_headers=True, forwarded_allow_ips=os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1"),
                log_level=os.environ.get("LOG_LEVEL", "info"))


if __name__ == "__main__":
    main()
