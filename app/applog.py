"""Application log: every event goes to stderr (container logs) and to the `logs` table, which
the Log tab shows. Users see their own entries; admins see everything.

Never log secrets: no passwords, verification codes or session cookies.
"""
import datetime as dt
import logging
import sys
import threading
import traceback
from pathlib import Path

from . import db

LEVELS = ("debug", "info", "warn", "error")
RETENTION_DAYS = 14
MAX_ROWS = 50_000
_count = 0
_lock = threading.Lock()


def _rel(path):
    """Store attachments relative to the data directory (so the volume can move)."""
    if not path:
        return None
    from .config import settings
    try:
        return str(Path(path).resolve().relative_to(settings.data_dir.resolve()))
    except ValueError:
        return None


def attachment_path(rel):
    """Absolute path of a stored attachment, refusing anything outside the data directory."""
    from .config import settings
    if not rel:
        return None
    root = settings.data_dir.resolve()
    p = (root / rel).resolve()
    if root not in p.parents or not p.is_file():
        return None
    return p


def log(message, level="info", user_id=None, watch_id=None, source="app", exc=False, attachment=None):
    """Record one event. exc=True appends the current exception's traceback; attachment is a
    screenshot of what the browser showed."""
    global _count
    level = level if level in LEVELS else "info"
    text = str(message)
    if exc:
        tb = traceback.format_exc()
        if tb and tb.strip() != "NoneType: None":
            text = f"{text}\n{tb.rstrip()}"
    who = f" user={user_id}" if user_id is not None else ""
    rel = _rel(attachment)
    shot = f" [screenshot: {rel}]" if rel else ""
    print(f"[{source}] {level.upper()}{who}: {text}{shot}", file=sys.stderr, flush=True)
    try:
        with db.tx() as con:
            con.execute("INSERT INTO logs(ts, level, source, user_id, watch_id, message, attachment)"
                        " VALUES (?,?,?,?,?,?,?)", (db.now(), level, source, user_id, watch_id, text[:8000], rel))
        with _lock:
            _count += 1
            prune = _count % 500 == 1
        if prune:
            _prune()
    except Exception:
        pass  # logging must never break the caller (e.g. DB not initialised yet)


def _prune():
    from .config import settings
    cutoff = (dt.datetime.now() - dt.timedelta(days=RETENTION_DAYS)).isoformat(timespec="seconds")
    with db.tx() as con:
        con.execute("DELETE FROM logs WHERE ts < ?", (cutoff,))
        con.execute("DELETE FROM logs WHERE id <= (SELECT MAX(id) FROM logs) - ?", (MAX_ROWS,))
        keep = {r[0] for r in con.execute("SELECT attachment FROM logs WHERE attachment IS NOT NULL")}
    # screenshots and sign-in recordings older than the log retention, unless still referenced
    old = dt.date.today() - dt.timedelta(days=RETENTION_DAYS)
    for folder in (settings.debug_dir / "shots", settings.debug_dir / "signin"):
        if not folder.is_dir():
            continue
        for f in folder.rglob("*"):
            try:
                if f.is_file() and dt.date.fromtimestamp(f.stat().st_mtime) < old and _rel(f) not in keep:
                    f.unlink()
            except OSError:
                pass


def info(msg, **kw):
    log(msg, "info", **kw)


def warn(msg, **kw):
    log(msg, "warn", **kw)


def error(msg, **kw):
    log(msg, "error", **kw)


class DBHandler(logging.Handler):
    """Forward Python logging (uvicorn errors, library warnings) into the log table."""

    def emit(self, record):
        if record.name.startswith("uvicorn.access"):
            return
        level = "error" if record.levelno >= logging.ERROR else "warn" if record.levelno >= logging.WARNING else "info"
        msg = record.getMessage()
        if record.exc_info:
            msg += "\n" + "".join(traceback.format_exception(*record.exc_info)).rstrip()
        try:
            with db.tx() as con:
                con.execute("INSERT INTO logs(ts, level, source, message) VALUES (?,?,?,?)",
                            (db.now(), level, record.name.split(".")[0], msg[:8000]))
        except Exception:
            pass


# Polled every few seconds by open pages (live status, log tail) and by Docker's health check:
# successful requests to these aren't worth an access-log line each
QUIET_PATHS = ("/api/status", "/api/log", "/health")


class QuietPolling(logging.Filter):
    """Drop uvicorn access-log lines for successful polling requests; errors still show."""

    def filter(self, record):
        args = record.args if isinstance(record.args, tuple) else ()
        if len(args) >= 5:   # (client, method, path, http version, status)
            path, status = str(args[2]).split("?")[0], args[4]
            if path in QUIET_PATHS and isinstance(status, int) and status < 400:
                return False
        return True


def install_python_logging():
    logging.getLogger("uvicorn.access").addFilter(QuietPolling())
    h = DBHandler(level=logging.WARNING)
    for name in ("uvicorn.error", "uvicorn", "fastapi", "asyncio"):
        logging.getLogger(name).addHandler(h)
