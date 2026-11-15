"""Optional: email the admins about warnings (or only errors) from the log, with the screenshot
and any raw JSON the entry mentions attached (SWA_EMAIL_WARNINGS).

Sent from a background thread so logging never waits on Gmail; the same message (numbers and
times ignored) is emailed at most once an hour, and at most MAX_PER_HOUR emails go out an hour.
"""
import html
import queue
import re
import threading
import time

MAX_PER_HOUR = 12
MAX_ATTACHMENT = 5 * 1024 * 1024
_q = queue.Queue()
_recent = {}          # message shape -> last sent (epoch)
_sent = []            # times of emails sent in the last hour
_started = False
_lock = threading.Lock()
SKIP_SOURCES = {"mail", "alerts"}   # never email about failing email


def wanted(level, source):
    from .config import settings
    mode = settings.email_warnings
    if source in SKIP_SOURCES or mode == "off":
        return False
    return level == "error" or (level == "warn" and mode == "warnings")


def shape(text):
    """A message with its numbers and file names blanked: repeats of the same problem match."""
    line = re.sub(r"debug/[\w./-]+", "<file>", text.split("\n", 1)[0])
    return re.sub(r"\d+", "#", line)[:200]


def files_for(text, attachment_rel):
    """The screenshot plus raw JSON files the message points at (debug/...json), inside the data dir."""
    from .applog import attachment_path
    out = []
    for rel in [attachment_rel, *re.findall(r"(debug/[\w./-]+\.json)", text)]:
        p = attachment_path(rel) if rel else None
        if p and p not in out:
            out.append(p)
    return out


def notify(level, source, text, attachment_rel, user_id=None):
    """Queue an email about this log entry if SWA_EMAIL_WARNINGS wants it (rate-limited)."""
    if not wanted(level, source):
        return False
    now = time.time()
    with _lock:
        key = shape(text)
        if now - _recent.get(key, 0) < 3600:
            return False
        _sent[:] = [t for t in _sent if now - t < 3600]
        if len(_sent) >= MAX_PER_HOUR:
            return False
        _recent[key] = now
        _sent.append(now)
    _q.put((level, source, text, attachment_rel, user_id))
    _start()
    return True


def _start():
    global _started
    with _lock:
        if _started:
            return
        _started = True
    threading.Thread(target=_worker, name="swa-alerts", daemon=True).start()


def _worker():
    while True:
        item = _q.get()
        try:
            send(*item)
        except Exception:
            pass


def send(level, source, text, attachment_rel, user_id=None):
    from . import db, mailer
    from .config import settings
    files = files_for(text, attachment_rel)
    small = [p for p in files if p.stat().st_size <= MAX_ATTACHMENT]
    skipped = [p.name for p in files if p not in small]
    who = f" (user {user_id})" if user_id is not None else ""
    body = (f"<p><b>{html.escape(level.upper())}</b> from <code>{html.escape(source)}</code>{who}:</p>"
            f"<pre style='white-space:pre-wrap;font-size:13px'>{html.escape(text[:6000])}</pre>"
            + (f"<p>Attached: {', '.join(html.escape(p.name) for p in small)}</p>" if small else "")
            + (f"<p>Too large to attach (see the Log tab): {html.escape(', '.join(skipped))}</p>" if skipped else "")
            + f"<p><a href='{html.escape(settings.base_url)}/log'>Open the log</a></p>")
    subject = f"SWA fare watcher {level}: {text.splitlines()[0][:90]}"
    for to in db.admin_emails():
        mailer.send(to, subject, body, attachments=small)
