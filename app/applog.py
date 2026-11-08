"""Application log: every event goes to stderr (container logs) and to the `logs` table, which
the Log tab shows. Users see their own entries; admins see everything.

Never log secrets: no passwords, verification codes or session cookies.
"""
import datetime as dt
import logging
import sys
import threading
import traceback

from . import db

LEVELS = ("debug", "info", "warn", "error")
RETENTION_DAYS = 14
MAX_ROWS = 50_000
_count = 0
_lock = threading.Lock()


def log(message, level="info", user_id=None, watch_id=None, source="app", exc=False):
    """Record one event. exc=True appends the current exception's traceback."""
    global _count
    level = level if level in LEVELS else "info"
    text = str(message)
    if exc:
        tb = traceback.format_exc()
        if tb and tb.strip() != "NoneType: None":
            text = f"{text}\n{tb.rstrip()}"
    who = f" user={user_id}" if user_id is not None else ""
    print(f"[{source}] {level.upper()}{who}: {text}", file=sys.stderr, flush=True)
    try:
        with db.tx() as con:
            con.execute("INSERT INTO logs(ts, level, source, user_id, watch_id, message) VALUES (?,?,?,?,?,?)",
                        (db.now(), level, source, user_id, watch_id, text[:8000]))
        with _lock:
            _count += 1
            prune = _count % 500 == 1
        if prune:
            _prune()
    except Exception:
        pass  # logging must never break the caller (e.g. DB not initialised yet)


def _prune():
    cutoff = (dt.datetime.now() - dt.timedelta(days=RETENTION_DAYS)).isoformat(timespec="seconds")
    with db.tx() as con:
        con.execute("DELETE FROM logs WHERE ts < ?", (cutoff,))
        con.execute("DELETE FROM logs WHERE id <= (SELECT MAX(id) FROM logs) - ?", (MAX_ROWS,))


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


def install_python_logging():
    h = DBHandler(level=logging.WARNING)
    for name in ("uvicorn.error", "uvicorn", "fastapi", "asyncio"):
        logging.getLogger(name).addHandler(h)
