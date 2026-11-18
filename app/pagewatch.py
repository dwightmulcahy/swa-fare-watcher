"""Watch Southwest's pages for changes that would break the app, and email the admins.

Three kinds of signal, each emailed once per distinct change (again after a week if it persists),
with a screenshot and the page HTML attached, whatever SWA_EMAIL_WARNINGS says:
- missing elements: a page the app works on lacks an element it relies on (form fields, the seat
  map's seat buttons, the trip page's seat link ...);
- structure: the set of form controls/landmarks on a page differs from the last time it worked
  (ids, names, roles, data-test; digits and CSS-module hashes ignored), an early heads-up for a
  redesign even while the app still copes;
- broken flow: a step that ran into something unexpected (a seat that didn't stick, no boarding
  position, fare data the parser doesn't know).
The page HTML stays on this server (data/debug/pages) and goes only to the admins.
"""
import datetime as dt
import hashlib
import json
import re

from . import applog, db, mailer
from .config import settings

RESEND_DAYS = 7

STRUCTURE_JS = """() => {
  const keep = (v) => (v || "").replace(/[0-9]+/g, "#").replace(/__[A-Za-z0-9]{5}\\b/g, "");
  const out = new Set();
  document.querySelectorAll("form, input, select, textarea, button, [role=dialog], [role=radio], [role=tab], [data-test], main, nav").forEach((e) => {
    const t = e.tagName.toLowerCase(), role = e.getAttribute("role") || "", id = keep(e.id),
          name = keep(e.getAttribute("name")), test = keep(e.getAttribute("data-test"));
    if (t === "button" && !id && !name && !test) return;      // plain buttons: too many, too varied
    out.add([t, role, id, name, test].filter(Boolean).join("|"));
  });
  return [...out].sort();
}"""


def enabled():
    return settings.page_watch


def _save_html(page, name):
    try:
        d = settings.debug_dir / "pages"
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{dt.datetime.now():%Y%m%d-%H%M%S}-{re.sub(r'[^a-z0-9]+', '-', name.lower())}.html"
        p.write_text(page.content())
        return p
    except Exception:
        return None


def _due(key):
    """Not emailed about this exact change in the last RESEND_DAYS."""
    last = db.get_state(f"pagewatch:{key}")
    if last and dt.datetime.fromisoformat(last) > dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=RESEND_DAYS):
        return False
    db.set_state(f"pagewatch:{key}", dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"))
    return True


def report(page_name, what, details, browser=None, files=(), user_id=None):
    """Log and email the admins about a change on `page_name` (deduplicated)."""
    if not enabled():
        return False
    key = hashlib.sha1(f"{page_name}|{what}|{details}".encode()).hexdigest()[:16]
    shot = getattr(browser, "capture", lambda tag: None)(f"pagewatch-{page_name}") if browser else None
    html = _save_html(browser.page, page_name) if browser is not None and getattr(browser, "page", None) else None
    applog.warn(f"Southwest page change ({page_name}): {what}. {details}", source="pagewatch", user_id=user_id,
                attachment=shot)
    if not _due(key):
        return False
    from pathlib import Path
    attach = []
    for p in (shot, html, *files):
        if p:
            p = Path(p) if Path(p).is_absolute() else settings.data_dir / p
            if p.exists():
                attach.append(p)
    body = mailer.render_notice(
        f"Southwest page change: {page_name}",
        [f"<b>{what}</b>", details,
         "The app may stop working on this page until it's updated. The screenshot and the page's HTML "
         "are attached (the HTML is from your own session: don't share it publicly)."],
        settings.base_url + "/log")
    for to in db.admin_emails():
        mailer.send(to, f"SWA page change: {page_name}: {what}"[:150], body, attachments=attach)
    return True


def check_elements(browser, page_name, selectors, user_id=None):
    """Report elements (label -> CSS selector) the app relies on that aren't on the page.
    Returns the missing labels."""
    if not enabled():
        return []
    page = browser.page
    missing = [k for k, sel in selectors.items() if not page.locator(sel).count()]
    if missing:
        report(page_name, "elements the app relies on are missing", ", ".join(missing), browser, user_id=user_id)
    return missing


def check_structure(browser, page_name, user_id=None):
    """Compare the page's form controls / landmarks with the last time; report a change."""
    if not enabled():
        return None
    try:
        now = set(browser.page.evaluate(STRUCTURE_JS))
    except Exception:
        return None
    key = f"pageshape:{page_name}"
    prev = db.get_state(key)
    db.set_state(key, json.dumps(sorted(now)))
    if prev is None:
        return None
    prev = set(json.loads(prev))
    gone, new = sorted(prev - now), sorted(now - prev)
    if not gone and not new:
        return None
    details = "; ".join(x for x in (f"gone: {', '.join(gone[:12])}" if gone else "",
                                    f"new: {', '.join(new[:12])}" if new else "") if x)
    report(page_name, "the page's structure changed", details, browser, user_id=user_id)
    return gone, new


def flow_failed(flow, error, browser=None, user_id=None, files=()):
    """A step met something unexpected on Southwest's side."""
    msg = re.sub(r"\d", "#", str(error))[:300]       # one email per kind of failure, not per seat
    return report(flow, "a step didn't go as expected", f"{error.__class__.__name__}: {msg}", browser,
                  files=files, user_id=user_id)
