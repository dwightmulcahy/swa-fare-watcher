"""SQLite storage."""
import datetime as dt
import sqlite3
from contextlib import contextmanager

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY,
  username TEXT NOT NULL UNIQUE COLLATE NOCASE,
  email TEXT NOT NULL DEFAULT '',
  pw_hash TEXT NOT NULL,
  is_admin INTEGER NOT NULL DEFAULT 0,
  active INTEGER NOT NULL DEFAULT 1,
  must_change_pw INTEGER NOT NULL DEFAULT 1,
  max_watches INTEGER,
  max_searches_per_day INTEGER,
  expires_at TEXT,
  swa_state BLOB,
  swa_state_updated TEXT,
  swa_status TEXT,
  swa_status_at TEXT,
  created_at TEXT NOT NULL,
  last_login TEXT
);
CREATE TABLE IF NOT EXISTS sessions(
  token_hash TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  csrf TEXT NOT NULL,
  expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS watches(
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  origin TEXT NOT NULL,
  destination TEXT NOT NULL,
  dates TEXT,
  days_from INTEGER,
  days_to INTEGER,
  weekdays TEXT,
  adults INTEGER NOT NULL DEFAULT 1,
  max_stops INTEGER,
  max_duration_min INTEGER,
  depart_after TEXT,
  depart_before TEXT,
  max_price REAL,
  max_points INTEGER,
  min_cpp REAL NOT NULL DEFAULT 1.3,
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  last_run_at TEXT,
  last_status TEXT
);
CREATE TABLE IF NOT EXISTS fares(
  id INTEGER PRIMARY KEY,
  watch_id INTEGER NOT NULL REFERENCES watches(id) ON DELETE CASCADE,
  ts TEXT NOT NULL,
  date TEXT NOT NULL,
  flight_key TEXT NOT NULL,
  flights TEXT, depart TEXT, arrive TEXT,
  stops INTEGER, duration INTEGER,
  cash REAL, points REAL, points_taxes REAL, cpp REAL,
  matched INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS fares_watch_ts ON fares(watch_id, ts);
CREATE TABLE IF NOT EXISTS alerted(
  watch_id INTEGER NOT NULL REFERENCES watches(id) ON DELETE CASCADE,
  key TEXT NOT NULL, cash REAL, points REAL,
  PRIMARY KEY(watch_id, key)
);
CREATE TABLE IF NOT EXISTS usage(
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  day TEXT NOT NULL,
  searches INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(user_id, day)
);
CREATE TABLE IF NOT EXISTS code_requests(
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  code TEXT,
  status TEXT NOT NULL DEFAULT 'waiting'   -- waiting, submitted, used, expired
);
CREATE TABLE IF NOT EXISTS runs(
  id INTEGER PRIMARY KEY,
  watch_id INTEGER NOT NULL REFERENCES watches(id) ON DELETE CASCADE,
  started TEXT NOT NULL,
  finished TEXT,
  searches INTEGER NOT NULL DEFAULT 0,
  failures INTEGER NOT NULL DEFAULT 0,
  matches INTEGER NOT NULL DEFAULT 0,
  note TEXT
);
"""


def now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def today() -> str:
    return dt.date.today().isoformat()


def connect() -> sqlite3.Connection:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(settings.db_path, timeout=30, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    return con


@contextmanager
def tx():
    con = connect()
    try:
        con.execute("BEGIN IMMEDIATE")
        yield con
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise
    finally:
        con.close()


def query(sql, args=()):
    con = connect()
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def one(sql, args=()):
    rows = query(sql, args)
    return rows[0] if rows else None


# Columns added after the first release; added to existing databases on start.
MIGRATIONS = {
    "users": {
        "swa_creds": "BLOB",                     # encrypted {"username", "password"}
        "auto_login": "INTEGER NOT NULL DEFAULT 0",
        "auto_login_status": "TEXT",
        "auto_login_at": "TEXT",
        "auto_login_day": "TEXT",                # day of the attempt counter below
        "auto_login_attempts": "INTEGER NOT NULL DEFAULT 0",
        "auto_login_failures": "INTEGER NOT NULL DEFAULT 0",   # consecutive
    },
}


def init():
    con = connect()
    try:
        con.executescript(SCHEMA)
        for table, cols in MIGRATIONS.items():
            have = {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}
            for name, decl in cols.items():
                if name not in have:
                    con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    finally:
        con.close()


# ---------- domain helpers ----------
def user_expired(user) -> bool:
    return bool(user["expires_at"]) and user["expires_at"] < today()


def user_can_run(user) -> bool:
    return bool(user["active"]) and not user_expired(user)


def searches_today(user_id) -> int:
    row = one("SELECT searches FROM usage WHERE user_id=? AND day=?", (user_id, today()))
    return row["searches"] if row else 0


def add_searches(con, user_id, n):
    con.execute(
        "INSERT INTO usage(user_id, day, searches) VALUES (?,?,?) "
        "ON CONFLICT(user_id, day) DO UPDATE SET searches = searches + excluded.searches",
        (user_id, today(), n),
    )
