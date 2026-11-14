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
CREATE TABLE IF NOT EXISTS calendar_fares(
  id INTEGER PRIMARY KEY,
  watch_id INTEGER NOT NULL REFERENCES watches(id) ON DELETE CASCADE,
  ts TEXT NOT NULL,
  leg TEXT NOT NULL,
  date TEXT NOT NULL,
  cash REAL,
  points REAL
);
CREATE INDEX IF NOT EXISTS calendar_watch ON calendar_fares(watch_id, leg, date, ts);
CREATE TABLE IF NOT EXISTS logs(
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  level TEXT NOT NULL,
  source TEXT NOT NULL,
  user_id INTEGER,
  watch_id INTEGER,
  message TEXT NOT NULL,
  attachment TEXT
);
CREATE INDEX IF NOT EXISTS logs_user ON logs(user_id, id);
CREATE TABLE IF NOT EXISTS digest_hits(
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created TEXT NOT NULL,
  hit TEXT NOT NULL                          -- JSON of the alert, as the instant email would show it
);
CREATE TABLE IF NOT EXISTS seat_jobs(
  id INTEGER PRIMARY KEY,
  watch_id INTEGER NOT NULL REFERENCES watches(id) ON DELETE CASCADE,
  leg TEXT NOT NULL,
  opens_at TEXT NOT NULL,                    -- UTC ISO: when seat selection opens for that flight
  status TEXT NOT NULL DEFAULT 'pending',    -- pending | running | done | unchanged | failed
  note TEXT,
  updated TEXT
);
CREATE TABLE IF NOT EXISTS searched(       -- when each watch date was last fully searched
  watch_id INTEGER NOT NULL REFERENCES watches(id) ON DELETE CASCADE,
  leg TEXT NOT NULL,
  date TEXT NOT NULL,
  ts TEXT NOT NULL,
  cash REAL,                                 -- cheapest seen then (any flight)
  points REAL,
  changed_at TEXT,                           -- last time those cheapest prices moved
  PRIMARY KEY (watch_id, leg, date)
);
CREATE TABLE IF NOT EXISTS app_state(
  key TEXT PRIMARY KEY,
  value TEXT
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
        "blocked_until": "TEXT",                 # scheduled runs paused after a block page, until then
        "blocked_reason": "TEXT",
        "alert_mode": "TEXT NOT NULL DEFAULT 'instant'",   # instant | daily | weekly
        "digest_sent_at": "TEXT",
    },
    "logs": {
        "attachment": "TEXT",                    # screenshot, relative to the data directory
    },
    "watches": {
        "trip_type": "TEXT NOT NULL DEFAULT 'oneway'",   # oneway | roundtrip
        "ret_dates": "TEXT",                     # explicit return dates, or ...
        "nights_min": "INTEGER",                 # ... stay length range after each outbound date
        "nights_max": "INTEGER",
        "ret_depart_after": "TEXT",
        "ret_depart_before": "TEXT",
        "mix_payment": "INTEGER NOT NULL DEFAULT 1",     # pick cash or points per leg
        "same_airports": "INTEGER NOT NULL DEFAULT 0",   # round trip must return between the same airports
        "date_from": "TEXT",                     # "between dates": fixed first and last departure day
        "date_to": "TEXT",
        "booked": "TEXT",                        # JSON [{leg, date, flights, cash, points, time, from}]
        "kind": "TEXT NOT NULL DEFAULT 'fare'",  # fare (find deals) | rebook (booked fare drops) | seats
        "booking_conf": "TEXT",                  # confirmation number of the booked trip
        "booking_first": "TEXT",                 # passenger name as on the booking (for lookup)
        "booking_last": "TEXT",
        "seat_auto": "INTEGER NOT NULL DEFAULT 0",   # pick seats when selection opens (48 h before)
        "seat_status": "TEXT",                   # seats.STATUSES key
        "seat_prefs": "TEXT",                    # JSON, see seats.DEFAULT_PREFS
    },
    "fares": {
        "leg": "TEXT NOT NULL DEFAULT 'out'",    # out | ret
        "origin": "TEXT",                        # airports of this flight (watches can have several)
        "dest": "TEXT",
        "via": "TEXT",                           # stop airports, space-separated ("MCO HOU")
    },
    "calendar_fares": {
        "origin": "TEXT",
        "dest": "TEXT",
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
        _split_booked(con)
    finally:
        con.close()


def _split_booked(con):
    """Once: fare watches that carried a booked fare become a fare watch plus a separate rebook
    watch for the same route (watch kinds replaced the 'Already booked?' section)."""
    import json
    if con.execute("SELECT 1 FROM app_state WHERE key='kinds_split'").fetchone():
        return
    rows = con.execute("SELECT * FROM watches WHERE kind='fare' AND booked IS NOT NULL AND booked<>''").fetchall()
    for w in rows:
        paid = [b for b in json.loads(w["booked"] or "[]") if b.get("cash") is not None or b.get("points") is not None]
        if paid:
            o, d = w["origin"].split(",")[0], w["destination"].split(",")[0]
            con.execute(
                "INSERT INTO watches(user_id, created_at, name, origin, destination, adults, trip_type, active,"
                " kind, booked, dates, mix_payment, same_airports) VALUES (?,?,?,?,?,?,?,?,'rebook',?,?,1,1)",
                (w["user_id"], w["created_at"], f"{w['name']} (booked)"[:60], o, d, w["adults"],
                 "roundtrip" if any(b["leg"] == "ret" for b in paid) else "oneway", w["active"],
                 json.dumps(paid), ",".join(b["date"] for b in paid if b["leg"] == "out")))
        con.execute("UPDATE watches SET booked=NULL WHERE id=?", (w["id"],))
    con.execute("INSERT OR REPLACE INTO app_state VALUES ('kinds_split', '1')")
    con.commit()


def sync_seat_jobs(watch_id, now=None):
    """(Re)plan the seat-selection jobs for a watch from its booked flights; jobs already run are kept."""
    import datetime as dt
    import json

    from . import seats
    w = one("SELECT * FROM watches WHERE id=?", (watch_id,))
    now = now or dt.datetime.now(dt.timezone.utc)
    with tx() as con:
        con.execute("DELETE FROM seat_jobs WHERE watch_id=? AND status='pending'", (watch_id,))
        if not w or not w["seat_auto"] or not w["active"]:
            return []
        done = {r["leg"] for r in con.execute("SELECT leg FROM seat_jobs WHERE watch_id=?", (watch_id,))}
        made = []
        for b in json.loads(w["booked"] or "[]"):
            opens = seats.opens_at(b.get("date"), b.get("time"), b.get("from"))
            if not opens or b["leg"] in done:
                continue
            departs = opens + dt.timedelta(hours=seats.OPEN_HOURS)
            if departs <= now + dt.timedelta(hours=1):
                continue                      # too late to matter
            con.execute("INSERT INTO seat_jobs(watch_id, leg, opens_at, updated) VALUES (?,?,?,?)",
                        (watch_id, b["leg"], opens.isoformat(timespec="seconds"), now.isoformat(timespec="seconds")))
            made.append((b["leg"], opens))
        return made


def get_state(key, default=None):
    row = one("SELECT value FROM app_state WHERE key=?", (key,))
    return row["value"] if row else default


def set_state(key, value):
    with tx() as con:
        con.execute("INSERT OR REPLACE INTO app_state VALUES (?,?)", (key, value))


def admin_emails():
    return [r["email"] for r in query("SELECT email FROM users WHERE is_admin=1 AND active=1 AND email<>''")]


def backup(keep=None, day=None):
    """Copy the live database to <data>/backups/app-YYYY-MM-DD.db (once a day, consistent even
    mid-write) and keep the newest `keep` copies. Returns the new file, or None if nothing to do."""
    keep = settings.backup_keep if keep is None else keep
    if keep <= 0:
        return None
    folder = settings.data_dir / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"app-{day or today()}.db"
    made = None
    if not path.exists():
        tmp = path.with_name(path.name + ".tmp")
        src, dst = connect(), sqlite3.connect(tmp)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        tmp.chmod(0o600)
        tmp.replace(path)
        made = path
    for old in sorted(folder.glob("app-*.db"))[:-keep]:
        old.unlink()
    return made


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
