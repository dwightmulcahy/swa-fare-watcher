"""FastAPI web app: login, user watches, Southwest session upload, admin panel."""
import datetime as dt
import hmac
import json
import mimetypes
import re
import sys
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote_plus, urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import airports, applog, buildinfo, db, seats, swa
from .config import settings
from .mailer import fmt_dur
from .security import (decrypt_json, encrypt_json, hash_password, new_token, temp_password,
                       token_hash, verify_password)
from .worker import worker as default_worker

HERE = Path(__file__).parent
mimetypes.add_type("text/javascript", ".mjs")  # module scripts must be served as JavaScript
COOKIE = "swa_session"
templates = Jinja2Templates(directory=str(HERE / "templates"))
templates.env.filters["dur"] = lambda m: fmt_dur(int(m or 0))
templates.env.filters["money"] = lambda v: "—" if v is None else f"${v:,.0f}"
templates.env.filters["pts"] = lambda v: "—" if v is None else f"{int(v):,}"
templates.env.filters["airport_names"] = lambda v: airports.describe(swa.airports(v))
def _stops_label(f):
    try:
        via = f["via"]
    except (KeyError, IndexError):
        via = ""
    return swa.stops_label(f["stops"], via or "")


templates.env.globals["stops_label"] = _stops_label
templates.env.globals["booked_of"] = lambda w: {b["leg"]: b for b in swa.booked(w)}
templates.env.globals["seats"] = seats
templates.env.globals["seat_prefs"] = seats.prefs_of
templates.env.globals["max_dates"] = settings.max_dates_per_watch
templates.env.filters["days_ahead"] = lambda n: (dt.date.today() + dt.timedelta(days=int(n))).isoformat()
templates.env.filters["airports"] = lambda v: "/".join(swa.airports(v))
templates.env.filters["when"] = lambda s: (s or "")[:16].replace("T", " ") or "—"
# Version label / About dialog: build info embedded in every page; "<" escaped so it can't close the tag
templates.env.globals["build_info_json"] = json.dumps(buildinfo.load()).replace("<", "\\u003c")
STARTED_AT = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
WEEKDAY_LABELS = list(zip(swa.WEEKDAYS, ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]))
IATA = re.compile(r"^[A-Z]{3}$")
TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class Redirect(Exception):
    def __init__(self, url):
        self.url = url


def bootstrap_admin():
    if db.one("SELECT 1 FROM users WHERE is_admin=1"):
        return
    pw = settings.admin_password or temp_password()
    with db.tx() as con:
        con.execute(
            "INSERT INTO users(username, email, pw_hash, is_admin, must_change_pw, created_at)"
            " VALUES (?,?,?,1,?,?)",
            (settings.admin_username, settings.gmail_user or "", hash_password(pw),
             0 if settings.admin_password else 1, db.now()))
    if not settings.admin_password:
        # printed to the container log only, never stored in the Log tab
        print(f"[setup] Created admin user '{settings.admin_username}' with temporary password: {pw}",
              file=sys.stderr, flush=True)
    applog.info(f"Created admin user '{settings.admin_username}'", source="setup")


def create_app(worker=None, start_worker=True):
    worker = worker or default_worker

    @asynccontextmanager
    async def lifespan(app):
        db.init()
        applog.install_python_logging()
        bootstrap_admin()
        if start_worker:
            worker.start(scheduler=settings.scheduler)
        yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.worker = worker
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
    login_failures = defaultdict(list)

    @app.exception_handler(Redirect)
    async def _redirect(request, exc):
        return RedirectResponse(exc.url, status_code=303)

    @app.middleware("http")
    async def security_headers(request, call_next):
        resp = await call_next(request)
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault("Content-Security-Policy",
                                "default-src 'self'; style-src 'self'; script-src 'self'; "
                                "img-src 'self' data:; form-action 'self'; frame-ancestors 'none'")
        return resp

    # ---------- auth helpers ----------
    def session(request):
        tok = request.cookies.get(COOKIE)
        if not tok:
            return None, None
        s = db.one("SELECT * FROM sessions WHERE token_hash=?", (token_hash(tok),))
        if not s or s["expires_at"] < db.now():
            return None, None
        u = db.one("SELECT * FROM users WHERE id=?", (s["user_id"],))
        if not u or not db.user_can_run(u):
            return None, None
        return u, s

    def require(request, admin=False, allow_pw_change=False):
        u, s = session(request)
        if not u:
            raise Redirect("/login")
        if u["must_change_pw"] and not allow_pw_change:
            raise Redirect("/password")
        if admin and not u["is_admin"]:
            raise Redirect("/?err=Admins+only")
        return u, s

    async def form(request, s):
        f = await request.form()
        if not hmac.compare_digest(str(f.get("csrf", "")), s["csrf"]):
            raise Redirect("/?err=Form+expired.+Try+again.")
        return f

    def pending_code(u):
        if not u:
            return None
        return db.one("SELECT * FROM code_requests WHERE user_id=? AND status='waiting' AND expires_at > ?"
                      " ORDER BY id DESC LIMIT 1", (u["id"], db.now()))

    def render(request, name, user=None, sess=None, status=200, **ctx):
        ctx.setdefault("code_request", pending_code(user))
        ctx.update(user=user, csrf=sess["csrf"] if sess else "", today_str=db.today(), now_str=db.now(),
                   msg=request.query_params.get("ok"), err=ctx.get("err") or request.query_params.get("err"))
        return templates.TemplateResponse(request, name, ctx, status_code=status)

    def start_session(user_id):
        tok = new_token()
        exp = (dt.datetime.now() + dt.timedelta(days=settings.session_days)).isoformat(timespec="seconds")
        with db.tx() as con:
            con.execute("DELETE FROM sessions WHERE expires_at < ?", (db.now(),))
            con.execute("INSERT INTO sessions VALUES (?,?,?,?)", (token_hash(tok), user_id, new_token(), exp))
            con.execute("UPDATE users SET last_login=? WHERE id=?", (db.now(), user_id))
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(COOKIE, tok, max_age=settings.session_days * 86400, httponly=True,
                        samesite="lax", secure=settings.cookie_secure)
        return resp

    @app.post("/resume")
    async def resume(request: Request):
        """Lift the pause put on scheduled runs after Southwest blocked a request."""
        user, sess = require(request)
        await form(request, sess)
        with db.tx() as con:
            con.execute("UPDATE users SET blocked_until=NULL WHERE id=?", (user["id"],))
        applog.info("Scheduled runs resumed by the user after a block pause", user_id=user["id"], source="run")
        raise Redirect("/?ok=Scheduled+runs+resumed")

    # ---------- public ----------
    @app.get("/health")
    @app.get("/healthz")
    def health():
        """Container health: the database answers and the worker threads are alive (503 if not)."""
        db.one("SELECT 1")
        worker = getattr(app.state, "worker", None)
        ok = worker is None or worker.alive()
        last = db.one("SELECT MAX(finished) f FROM runs")["f"]
        return JSONResponse({"status": "ok" if ok else "worker stopped", "last_run_finished": last},
                            status_code=200 if ok else 503)

    @app.get("/api/status")
    def api_status(request: Request):
        u, _ = session(request)
        if not u:
            return JSONResponse({"error": "signed out"}, status_code=401)
        return JSONResponse(app.state.worker.snapshot(u["id"]), headers={"Cache-Control": "no-store"})

    # ---------- log ----------
    def log_query(u, params, after=0, limit=300):
        where, args = ["l.id > ?"], [after]
        if not u["is_admin"]:
            where.append("l.user_id = ?")
            args.append(u["id"])
        else:
            who = params.get("user", "")
            if who == "system":
                where.append("l.user_id IS NULL")
            elif who.isdigit():
                where.append("l.user_id = ?")
                args.append(int(who))
        level = params.get("level", "")
        if level in ("warn", "error"):
            where.append("l.level IN ('warn','error')" if level == "warn" else "l.level = 'error'")
        q = params.get("q", "").strip()
        if q:
            where.append("l.message LIKE ? ESCAPE '\\'")
            args.append("%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
        wid = params.get("watch", "")
        if wid.isdigit():
            where.append("l.watch_id = ?")
            args.append(int(wid))
        rows = db.query(f"SELECT l.*, u.username FROM logs l LEFT JOIN users u ON u.id = l.user_id "
                        f"WHERE {' AND '.join(where)} ORDER BY l.id DESC LIMIT ?", (*args, limit))
        out = []
        for r in rows:
            d = dict(r)
            d["shot_url"] = f"/log/shot/{d['id']}" if d.pop("attachment", None) else None
            out.append(d)
        return out

    @app.get("/log/shot/{log_id}")
    def log_shot(request: Request, log_id: int):
        u, _ = require(request)
        row = db.one("SELECT user_id, attachment FROM logs WHERE id=?", (log_id,))
        if not row or not (u["is_admin"] or row["user_id"] == u["id"]):
            return JSONResponse({"error": "not found"}, status_code=404)
        path = applog.attachment_path(row["attachment"])
        if not path:
            return JSONResponse({"error": "screenshot no longer available"}, status_code=404)
        return FileResponse(path, media_type="image/png", headers={"Cache-Control": "private, max-age=86400"})

    @app.get("/log", response_class=HTMLResponse)
    def log_page(request: Request):
        u, s = require(request)
        params = dict(request.query_params)
        users = db.query("SELECT id, username FROM users ORDER BY username") if u["is_admin"] else []
        watches = db.query("SELECT id, name FROM watches WHERE user_id=? ORDER BY name", (u["id"],)) \
            if not u["is_admin"] else db.query("SELECT id, name FROM watches ORDER BY name")
        return render(request, "log.html", u, s, rows=log_query(u, params), params=params, users=users,
                      watches=watches)

    @app.get("/api/log")
    def api_log(request: Request):
        u, _ = session(request)
        if not u:
            return JSONResponse({"error": "signed out"}, status_code=401)
        params = dict(request.query_params)
        after = int(params.get("after", "0") or 0) if str(params.get("after", "0")).isdigit() else 0
        return JSONResponse({"rows": log_query(u, params, after=after, limit=200)},
                            headers={"Cache-Control": "no-store"})

    @app.get("/api/about")
    def about():
        # server start time for the About dialog (no dependency versions)
        return JSONResponse({"started_at": STARTED_AT}, headers={"Cache-Control": "no-store"})

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request):
        if session(request)[0]:
            raise Redirect("/")
        return render(request, "login.html")

    @app.post("/login")
    async def login(request: Request):
        ip = request.client.host if request.client else "?"
        recent = [t for t in login_failures[ip] if t > time.time() - 900]
        login_failures[ip] = recent
        if len(recent) >= 10:
            return render(request, "login.html", err="Too many attempts. Try again in 15 minutes.", status=429)
        f = await request.form()
        u = db.one("SELECT * FROM users WHERE username=?", (str(f.get("username", "")).strip(),))
        ok = u is not None and verify_password(str(f.get("password", "")), u["pw_hash"])
        if not ok:
            recent.append(time.time())
            return render(request, "login.html", err="Wrong username or password.", status=401)
        if not u["active"]:
            return render(request, "login.html", err="This account has been revoked.", status=403)
        if db.user_expired(u):
            return render(request, "login.html", err="This account has expired.", status=403)
        login_failures.pop(ip, None)
        return start_session(u["id"])

    @app.post("/logout")
    async def logout(request: Request):
        u, s = session(request)
        if s:
            await form(request, s)
            with db.tx() as con:
                con.execute("DELETE FROM sessions WHERE token_hash=?", (s["token_hash"],))
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(COOKIE)
        return resp

    @app.get("/password", response_class=HTMLResponse)
    def password_page(request: Request):
        u, s = require(request, allow_pw_change=True)
        return render(request, "password.html", u, s)

    @app.post("/password")
    async def change_password(request: Request):
        u, s = require(request, allow_pw_change=True)
        f = await form(request, s)
        cur, new, confirm = (str(f.get(k, "")) for k in ("current", "new", "confirm"))
        err = None
        if not verify_password(cur, u["pw_hash"]):
            err = "Current password is wrong."
        elif len(new) < 10:
            err = "New password must be at least 10 characters."
        elif new != confirm:
            err = "New passwords don't match."
        elif new == cur:
            err = "Choose a different password."
        if err:
            return render(request, "password.html", u, s, err=err, status=400)
        with db.tx() as con:
            con.execute("UPDATE users SET pw_hash=?, must_change_pw=0 WHERE id=?", (hash_password(new), u["id"]))
            con.execute("DELETE FROM sessions WHERE user_id=? AND token_hash<>?", (u["id"], s["token_hash"]))
        raise Redirect("/?ok=Password+changed")

    # ---------- watches ----------
    def watch_or_404(u, wid):
        w = db.one("SELECT * FROM watches WHERE id=? AND user_id=?", (wid, u["id"]))
        if not w:
            raise Redirect("/?err=Watch+not+found")
        return w

    def limits_for(u):
        active = db.query("SELECT * FROM watches WHERE user_id=? AND active=1", (u["id"],))
        return {
            "watches": len(active), "max_watches": u["max_watches"],
            "per_day": sum(swa.searches_per_run(w) for w in active),
            "max_searches": u["max_searches_per_day"],
            "today": db.searches_today(u["id"]),
        }

    def check_limits(u, w, exclude_id=None):
        if not w["active"]:
            return None
        others = [x for x in db.query("SELECT * FROM watches WHERE user_id=? AND active=1", (u["id"],))
                  if x["id"] != exclude_id]
        if u["max_watches"] is not None and len(others) + 1 > u["max_watches"]:
            return f"You can have at most {u['max_watches']} active watch(es). Pause or delete one first."
        need = swa.searches_per_run(w)
        if need == 0:
            return "No upcoming dates match. Check the dates and weekdays."
        total = need + sum(swa.searches_per_run(x) for x in others)
        cap = u["max_searches_per_day"]
        if cap is not None and total > cap:
            return (f"This watch needs {need} searches per day (2 per date); with your other watches that's "
                    f"{total}, over your limit of {cap}. Narrow the dates or weekdays.")
        return None

    def parse_watch(f):
        errs = []

        def opt_int(k, lo=0, hi=10**7):
            v = str(f.get(k, "")).strip().replace(",", "")
            if not v:
                return None
            try:
                n = int(float(v))
            except ValueError:
                errs.append(f"{k.replace('_', ' ').capitalize()} must be a number.")
                return None
            if not lo <= n <= hi:
                errs.append(f"{k.replace('_', ' ').capitalize()} must be between {lo} and {hi}.")
            return n

        w = {
            "name": str(f.get("name", "")).strip()[:60],
            "origin": ",".join(swa.airports(str(f.get("origin", "")))),
            "destination": ",".join(swa.airports(str(f.get("destination", "")))),
            "dates": None, "days_from": None, "days_to": None, "date_from": None, "date_to": None,
            "weekdays": ",".join(d for d in swa.WEEKDAYS if f.get(f"wd_{d}")) or None,
            "adults": opt_int("adults", 1, 8) or 1,
            "max_stops": opt_int("max_stops", 0, 3),
            "max_duration_min": None,
            "depart_after": str(f.get("depart_after", "")).strip() or None,
            "depart_before": str(f.get("depart_before", "")).strip() or None,
            "max_price": opt_int("max_price", 1, 100000),
            "max_points": opt_int("max_points", 1, 10**7),
            "min_cpp": 1.3,
            "active": 1 if f.get("active", "1") == "1" else 0,
        }
        hours = str(f.get("max_duration_h", "")).strip()
        if hours:
            try:
                w["max_duration_min"] = int(round(float(hours) * 60))
            except ValueError:
                errs.append("Max duration must be a number of hours.")
        try:
            w["min_cpp"] = round(float(str(f.get("min_cpp", "1.3")).strip() or 1.3), 2)
        except ValueError:
            errs.append("Minimum ¢/point must be a number.")
        if not w["name"]:
            w["name"] = swa.route_label(w)
        for k, label in (("origin", "From"), ("destination", "To")):
            codes = swa.airports(w[k])
            if not codes or not all(IATA.match(c) for c in codes):
                errs.append(f"{label} must be 3-letter airport codes, separated by commas.")
            elif len(codes) > swa.MAX_AIRPORTS:
                errs.append(f"{label}: at most {swa.MAX_AIRPORTS} airports.")
        if not errs and not swa.routes(w):
            errs.append("Origin and destination must differ.")
        for k in ("depart_after", "depart_before"):
            if w[k] and not TIME.match(w[k]):
                errs.append("Departure times must be HH:MM.")
        if f.get("date_mode") == "between":
            try:
                a = dt.date.fromisoformat(str(f.get("between_start", "")))
                b = dt.date.fromisoformat(str(f.get("between_end", "")))
            except ValueError:
                errs.append("Between dates needs a first and last departure date.")
            else:
                if b < a:
                    errs.append("Between dates: the last date must be on or after the first.")
                elif b < dt.date.today():
                    errs.append("Between dates: the last date has already passed.")
                elif (b - a).days > 365:
                    errs.append("Between dates: at most a year apart.")
                w["date_from"], w["date_to"] = a.isoformat(), b.isoformat()
        elif f.get("date_mode") == "window":
            # picked as calendar dates, stored as days ahead so the window rolls forward daily
            for k, src in (("days_from", "window_start"), ("days_to", "window_end")):
                if f.get(src):
                    try:
                        days = (dt.date.fromisoformat(str(f[src])) - dt.date.today()).days
                    except ValueError:
                        days = None
                    w[k] = days if days is not None and 0 <= days <= 365 else None
                else:
                    w[k] = opt_int(k, 0, 365)
            if w["days_from"] is None or w["days_to"] is None or w["days_to"] < w["days_from"]:
                errs.append("Rolling window needs a start and end date between today and a year ahead, "
                            "with the end on or after the start.")
        else:
            raw = [d.strip() for d in re.split(r"[,\s]+", str(f.get("dates", ""))) if d.strip()]
            try:
                parsed = sorted({dt.date.fromisoformat(d) for d in raw})
            except ValueError:
                parsed = None
                errs.append("Dates must be YYYY-MM-DD, separated by commas.")
            if parsed is not None:
                if not parsed:
                    errs.append("Enter at least one travel date.")
                elif len(parsed) > settings.max_dates_per_watch:
                    errs.append(f"At most {settings.max_dates_per_watch} departure dates ({len(parsed)} picked).")
                w["dates"] = ",".join(d.isoformat() for d in parsed) or None
        # round trip: searched as two one-ways (Southwest prices each direction separately)
        w["trip_type"] = "roundtrip" if f.get("trip_type") == "roundtrip" else "oneway"
        w["ret_dates"] = w["nights_min"] = w["nights_max"] = None
        w["ret_depart_after"] = str(f.get("ret_depart_after", "")).strip() or None
        w["ret_depart_before"] = str(f.get("ret_depart_before", "")).strip() or None
        w["mix_payment"] = 1 if f.get("mix_payment") else 0
        w["same_airports"] = 1 if f.get("same_airports") else 0
        if w["trip_type"] == "roundtrip":
            for k in ("ret_depart_after", "ret_depart_before"):
                if w[k] and not TIME.match(w[k]):
                    errs.append("Return departure times must be HH:MM.")
            if f.get("return_mode") == "dates":
                raw = [d.strip() for d in re.split(r"[,\s]+", str(f.get("ret_dates", ""))) if d.strip()]
                try:
                    parsed = sorted({dt.date.fromisoformat(d) for d in raw})
                    if not parsed:
                        errs.append("Enter at least one return date.")
                    elif len(parsed) > settings.max_dates_per_watch:
                        errs.append(f"At most {settings.max_dates_per_watch} return dates ({len(parsed)} picked).")
                    w["ret_dates"] = ",".join(d.isoformat() for d in parsed) or None
                except ValueError:
                    errs.append("Return dates must be YYYY-MM-DD, separated by commas.")
            else:
                w["nights_min"] = opt_int("nights_min", 0, 60)
                w["nights_max"] = opt_int("nights_max", 0, 60)
                if w["nights_min"] is None or w["nights_max"] is None or w["nights_max"] < w["nights_min"]:
                    errs.append("Stay length needs a minimum and maximum number of nights, with max ≥ min.")
            if not errs and not swa.trip_pairs(w):
                errs.append("No return date falls on or after an outbound date. Check the return dates or stay length.")
        else:
            w["ret_depart_after"] = w["ret_depart_before"] = None
        booked = []
        w["seat_auto"] = 1 if f.get("seat_auto") else 0
        conf = re.sub(r"[^A-Za-z0-9]", "", str(f.get("booking_conf", ""))).upper()
        w["booking_conf"] = conf or None
        w["booking_first"] = str(f.get("booking_first", "")).strip()[:30] or None
        w["booking_last"] = str(f.get("booking_last", "")).strip()[:30] or None
        status = str(f.get("seat_status", "none"))
        w["seat_status"] = status if status in seats.STATUSES else "none"
        pos = str(f.get("seat_position", "window"))
        w["seat_prefs"] = json.dumps({"position": pos if pos in seats.POSITIONS else "any",
                                      **{k: bool(f.get(f"seat_{k}")) for k in seats.WISHES}})
        if w["seat_auto"] and not (conf and len(conf) == 6 and w["booking_first"] and w["booking_last"]):
            errs.append("To pick seats automatically, enter the 6-character confirmation number and the "
                        "passenger's first and last name as on the booking.")
        origins = [x for x in re.split(r"[\s,]+", w["origin"]) if x]
        dests = [x for x in re.split(r"[\s,]+", w["destination"]) if x]
        for leg in ("out", "ret") if w["trip_type"] == "roundtrip" else ("out",):
            d = str(f.get(f"booked_{leg}_date", "")).strip()
            if not d:
                continue
            try:
                dt.date.fromisoformat(d)
            except ValueError:
                errs.append("Booked flight date must be YYYY-MM-DD.")
                continue
            cash = opt_int(f"booked_{leg}_cash", 1, 100_000)
            points = opt_int(f"booked_{leg}_points", 1, 10_000_000)
            t = str(f.get(f"booked_{leg}_time", "")).strip()
            frm = re.sub(r"[^A-Za-z]", "", str(f.get(f"booked_{leg}_from", ""))).upper()[:3]
            frm = frm or ((origins if leg == "out" else dests) or [""])[0]
            if t and not re.fullmatch(r"\d{2}:\d{2}", t):
                errs.append("Departure time must be HH:MM.")
                continue
            if cash is None and points is None and not w["seat_auto"]:
                errs.append("For a booked flight, enter what you paid (cash or points).")
                continue
            if w["seat_auto"] and not t:
                errs.append("To pick seats automatically, enter each booked flight's departure time.")
                continue
            if w["seat_auto"] and not seats.airport_tz(frm):
                errs.append(f"Unknown departure airport {frm or '(blank)'} for the booked flight.")
                continue
            booked.append({"leg": leg, "date": d, "cash": cash, "points": points, "time": t or None,
                           "from": frm or None,
                           "flights": re.sub(r"[^0-9/ ,]", "", str(f.get(f"booked_{leg}_flights", "")))[:40].strip()})
        if w["seat_auto"] and not booked:
            errs.append("To pick seats automatically, enter the booked flight's date and departure time.")
        w["booked"] = json.dumps(booked) if booked else None
        return w, errs

    def latest_legs(wid, day=None):
        """Most recent flights per leg and date, as {"out": {date: [rows]}, "ret": {...}}
        (optionally as they stood at the end of `day`)."""
        cond, args = ("AND substr(ts,1,10) <= ?", (wid, day, wid)) if day else ("", (wid, wid))
        rows = db.query(
            "SELECT f.* FROM fares f JOIN (SELECT leg, date, MAX(ts) ts FROM fares WHERE watch_id=? "
            + cond + " GROUP BY leg, date) l ON f.leg=l.leg AND f.date=l.date AND f.ts=l.ts WHERE f.watch_id=?",
            args)
        legs = {"out": {}, "ret": {}}
        for r in rows:
            legs.setdefault(r["leg"], {}).setdefault(r["date"], []).append(dict(r))
        return legs

    def fare_route(w, f):
        if f["origin"] and f["dest"]:
            return f["origin"], f["dest"]
        o, d = swa.airports(w["origin"])[0], swa.airports(w["destination"])[0]   # stored before multi-airport
        return (o, d) if f["leg"] == "out" else (d, o)

    def best_roundtrip(w):
        trips = swa.roundtrips(w, latest_legs(w["id"]))
        matched = [t for t in trips if t["matched"]]
        cash = min((t["cash"] for t in matched if t["cash"] is not None), default=None)
        points = min((t["points"] for t in matched if t["points"] is not None), default=None)
        return {"cash": cash, "points": points, "any_cash": min((t["cash"] for t in trips if t["cash"]), default=None),
                "n": len(trips)}

    def watch_ctx(w):
        return {"w": w, "weekdays": WEEKDAY_LABELS,
                "estimate": swa.searches_per_run(w) if (w.get("dates") or w.get("date_from") or w.get("days_to") is not None) else 0}

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        u, s = require(request)
        watches = db.query("SELECT * FROM watches WHERE user_id=? ORDER BY active DESC, id", (u["id"],))
        best = {}
        for w in watches:
            if swa.is_roundtrip(w):
                best[w["id"]] = best_roundtrip(w)
                continue
            best[w["id"]] = db.one(
                "SELECT MIN(CASE WHEN matched=1 THEN cash END) AS cash,"
                " MIN(CASE WHEN matched=1 THEN points END) AS points,"
                " MIN(cash) AS any_cash, COUNT(*) AS n FROM fares"
                " WHERE watch_id=? AND ts=(SELECT MAX(ts) FROM fares WHERE watch_id=?)", (w["id"], w["id"]))
        return render(request, "dashboard.html", u, s, watches=watches, best=best,
                      est={w["id"]: swa.searches_per_run(w) for w in watches},
                      limits=limits_for(u), run_status=app.state.worker.status(u["id"]),
                      live=app.state.worker.snapshot(u["id"]),
                      daily_at=settings.daily_at, jitter=settings.daily_jitter_min,
                      next_run=app.state.worker.next_run_for(u["id"]))

    @app.get("/watches/new", response_class=HTMLResponse)
    def new_watch(request: Request):
        u, s = require(request)
        w = {"name": "", "origin": "", "destination": "", "dates": "", "days_from": 14, "days_to": 45,
             "date_from": None, "date_to": None,
             "weekdays": "", "adults": 1, "max_stops": 1, "max_duration_min": None, "depart_after": "",
             "depart_before": "", "max_price": None, "max_points": None, "min_cpp": 1.3, "active": 1,
             "trip_type": "oneway", "ret_dates": "", "nights_min": 2, "nights_max": 5,
             "ret_depart_after": "", "ret_depart_before": "", "mix_payment": 1, "same_airports": 0,
             "booked": None, "booking_conf": None, "booking_first": None, "booking_last": None,
             "seat_auto": 0, "seat_status": "none", "seat_prefs": None}
        return render(request, "watch_form.html", u, s, **watch_ctx(w), new=True, limits=limits_for(u))

    @app.post("/watches")
    async def create_watch(request: Request):
        u, s = require(request)
        f = await form(request, s)
        w, errs = parse_watch(f)
        err = "; ".join(errs) if errs else check_limits(u, w)
        if err:
            return render(request, "watch_form.html", u, s, **watch_ctx(w), new=True,
                          limits=limits_for(u), err=err, status=400)
        cols = list(w)
        with db.tx() as con:
            wid = con.execute(
                f"INSERT INTO watches(user_id, created_at, {', '.join(cols)}) VALUES (?,?,{','.join('?' * len(cols))})",
                (u["id"], db.now(), *w.values())).lastrowid
        db.sync_seat_jobs(wid)
        raise Redirect(f"/watches/{wid}?ok=Watch+created")

    def _seat_jobs(wid):
        out = []
        for j in db.query("SELECT * FROM seat_jobs WHERE watch_id=? ORDER BY opens_at", (wid,)):
            opens = dt.datetime.fromisoformat(j["opens_at"]).astimezone()
            out.append({**dict(j), "opens_local": f"{opens:%a %b %-d, %H:%M}",
                        "leg_label": "Return" if j["leg"] == "ret" else "Outbound"})
        return out

    def _lows(trips):
        m = [x for x in trips if x["matched"]]
        return {"cash": min((x["cash"] for x in m if x["cash"]), default=None),
                "points": min((x["points"] for x in m if x["points"]), default=None),
                "any_cash": min((x["cash"] for x in trips if x["cash"]), default=None)}

    @app.get("/watches/{wid}", response_class=HTMLResponse)
    def watch_detail(request: Request, wid: int):
        u, s = require(request)
        w = watch_or_404(u, wid)
        last = db.one("SELECT MAX(ts) AS ts FROM fares WHERE watch_id=?", (wid,))["ts"]
        # latest results per date (dates can be searched in different runs)
        fares = db.query(
            "SELECT f.* FROM fares f JOIN (SELECT leg, date, MAX(ts) ts FROM fares WHERE watch_id=? GROUP BY leg, date) l"
            " ON f.leg=l.leg AND f.date=l.date AND f.ts=l.ts WHERE f.watch_id=?"
            " ORDER BY CASE f.leg WHEN 'out' THEN 0 ELSE 1 END, f.date, f.matched DESC, f.cash", (wid, wid))
        rt = swa.is_roundtrip(w)
        trips = swa.roundtrips(w, latest_legs(wid))[:40] if rt else []
        if rt:  # best matching round trip as it stood at the end of each day
            days = [r["day"] for r in db.query("SELECT DISTINCT substr(ts,1,10) day FROM fares WHERE watch_id=?"
                                               " ORDER BY day DESC LIMIT 30", (wid,))]
            trend, per = [], {}
            for d in days:
                t = swa.roundtrips(w, latest_legs(wid, d))
                trend.append({"day": d, **_lows(t)})
                pairs = {}
                for x in t:   # one series per date pair: that pair's best trip at the end of each day
                    pairs.setdefault((x["out_date"], x["ret_date"]), []).append(x)
                for k, xs in pairs.items():
                    per.setdefault(k, []).insert(0, {"day": d, **_lows(xs)})
            history = {"All dates": list(reversed(trend))}
            for (o, r), pts in sorted(per.items())[:60]:
                history[f"{o} → {r}"] = pts
        else:
            trend = db.query(
                "SELECT substr(ts,1,10) AS day, MIN(CASE WHEN matched=1 THEN cash END) AS cash,"
                " MIN(CASE WHEN matched=1 THEN points END) AS points, MIN(cash) AS any_cash"
                " FROM fares WHERE watch_id=? GROUP BY day ORDER BY day DESC LIMIT 30", (wid,))
            history = {"All dates": [dict(r) for r in reversed(trend)]}
            for r in db.query(
                    "SELECT date, substr(ts,1,10) AS day, MIN(CASE WHEN matched=1 THEN cash END) AS cash,"
                    " MIN(CASE WHEN matched=1 THEN points END) AS points, MIN(cash) AS any_cash"
                    " FROM fares WHERE watch_id=? AND leg='out' AND ts >= date('now', '-60 days')"
                    " GROUP BY date, day ORDER BY date, day", (wid,)):
                history.setdefault(r["date"], []).append({k: r[k] for k in ("day", "cash", "points", "any_cash")})
        runs = db.query("SELECT * FROM runs WHERE watch_id=? ORDER BY id DESC LIMIT 10", (wid,))
        legs = swa.search_legs(w)
        return render(request, "watch_detail.html", u, s, w=w, fares=fares, last=last, trend=trend,
                      history=history, seat_jobs=_seat_jobs(wid),
                      runs=runs, dates=[d for leg, d in legs if leg == "out"],
                      ret_dates=[d for leg, d in legs if leg == "ret"], rt=rt, trips=trips,
                      pairs=len(swa.trip_pairs(w)) if rt else 0,
                      run_status=app.state.worker.status(u["id"]),
                      book=lambda f: swa.booking_url(*fare_route(w, f), f["date"], w["adults"]),
                      fare_route=lambda f: "→".join(fare_route(w, f)),
                      multi=len(swa.routes(w)) > 1, searches=swa.searches_per_run(w),
                      recommend=lambda r: swa.recommend(r, w),
                      recommend_detail=lambda r: swa.recommend_detail(r, w))

    @app.get("/watches/{wid}/edit", response_class=HTMLResponse)
    def edit_watch(request: Request, wid: int):
        u, s = require(request)
        w = dict(watch_or_404(u, wid))
        return render(request, "watch_form.html", u, s, **watch_ctx(w), new=False, limits=limits_for(u))

    @app.post("/watches/{wid}")
    async def update_watch(request: Request, wid: int):
        u, s = require(request)
        watch_or_404(u, wid)
        f = await form(request, s)
        w, errs = parse_watch(f)
        err = "; ".join(errs) if errs else check_limits(u, w, exclude_id=wid)
        if err:
            return render(request, "watch_form.html", u, s, **watch_ctx({**w, "id": wid}), new=False,
                          limits=limits_for(u), err=err, status=400)
        with db.tx() as con:
            con.execute(f"UPDATE watches SET {', '.join(f'{k}=?' for k in w)} WHERE id=? AND user_id=?",
                        (*w.values(), wid, u["id"]))
        db.sync_seat_jobs(wid)
        raise Redirect(f"/watches/{wid}?ok=Watch+saved")

    @app.post("/watches/{wid}/toggle")
    async def toggle_watch(request: Request, wid: int):
        u, s = require(request)
        w = dict(watch_or_404(u, wid))
        await form(request, s)
        if not w["active"]:
            err = check_limits(u, {**w, "active": 1}, exclude_id=wid)
            if err:
                raise Redirect("/?err=" + quote_plus(err))
        with db.tx() as con:
            con.execute("UPDATE watches SET active=? WHERE id=?", (0 if w["active"] else 1, wid))
        db.sync_seat_jobs(wid)
        raise Redirect("/?ok=" + ("Watch+paused" if w["active"] else "Watch+resumed"))

    @app.post("/watches/{wid}/delete")
    async def delete_watch(request: Request, wid: int):
        u, s = require(request)
        watch_or_404(u, wid)
        await form(request, s)
        with db.tx() as con:
            con.execute("DELETE FROM watches WHERE id=? AND user_id=?", (wid, u["id"]))
        raise Redirect("/?ok=Watch+deleted")

    @app.post("/watches/{wid}/run")
    async def run_watch(request: Request, wid: int):
        u, s = require(request)
        w = watch_or_404(u, wid)
        await form(request, s)
        if not w["active"]:
            raise Redirect(f"/watches/{wid}?err=Resume+the+watch+first")
        need, cap = swa.searches_per_run(w), u["max_searches_per_day"]
        if cap is not None and db.searches_today(u["id"]) + 2 > cap:
            raise Redirect(f"/watches/{wid}?err=Daily+search+limit+reached")
        app.state.worker.enqueue(u["id"], [wid])
        raise Redirect(f"/watches/{wid}?ok=Search+queued+({need}+searches).+Refresh+in+a+few+minutes.")

    @app.post("/run/cancel")
    async def cancel_run(request: Request):
        u, s = require(request)
        await form(request, s)
        state = app.state.worker.cancel(u["id"])
        ref = request.headers.get("referer") or ""
        back = urlsplit(ref).path if ref.startswith(str(request.base_url)) else ""
        back = back if back.startswith("/") and not back.startswith("//") else "/"
        msg = {"running": "Cancelling+after+the+current+search.+Results+so+far+are+saved.",
               "queued": "Queued+search+cancelled."}.get(state)
        raise Redirect(back + (f"?ok={msg}" if msg else "?err=Nothing+is+running"))

    # ---------- settings ----------
    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request):
        u, s = require(request)
        creds = decrypt_json(u["swa_creds"]) or {}
        attempts = u["auto_login_attempts"] if u["auto_login_day"] == db.today() else 0
        return render(request, "settings.html", u, s, limits=limits_for(u), digest_at=settings.digest_at, creds_username=creds.get("username"),
                      auto_login_available=settings.auto_login, signin_attempts_today=attempts,
                      signin_status=app.state.worker.status(u["id"]),
                      code_wait_minutes=max(1, settings.code_wait_seconds // 60))

    @app.post("/settings/auto-login")
    async def save_auto_login(request: Request):
        u, s = require(request)
        f = await form(request, s)
        old = decrypt_json(u["swa_creds"]) or {}
        username = str(f.get("swa_username", "")).strip()
        password = str(f.get("swa_password", ""))
        enable = 1 if f.get("auto_login") else 0
        if not username:
            raise Redirect("/settings?err=" + quote_plus("Enter your Rapid Rewards number or username.") + "#auto")
        if not password:
            if not old.get("password") or old.get("username") != username:
                raise Redirect("/settings?err=" + quote_plus("Enter your Southwest password.") + "#auto")
            password = old["password"]
        changed = old.get("username") != username or old.get("password") != password
        with db.tx() as con:
            con.execute("UPDATE users SET swa_creds=?, auto_login=? WHERE id=?",
                        (encrypt_json({"username": username, "password": password}), enable, u["id"]))
            if changed or (enable and not u["auto_login"]):
                con.execute("UPDATE users SET auto_login_failures=0, auto_login_status=? WHERE id=?",
                            ("Saved; not tried yet" if changed else "Turned on; not tried yet", u["id"]))
        raise Redirect("/settings?ok=" + quote_plus("Automatic sign-in " + ("on" if enable else "saved but off")) + "#auto")

    @app.post("/settings/auto-login/remove")
    async def remove_auto_login(request: Request):
        u, s = require(request)
        await form(request, s)
        with db.tx() as con:
            con.execute("UPDATE users SET swa_creds=NULL, auto_login=0, auto_login_status=NULL, "
                        "auto_login_failures=0 WHERE id=?", (u["id"],))
        raise Redirect("/settings?ok=" + quote_plus("Saved Southwest username and password removed") + "#auto")

    @app.post("/settings/auto-login/test")
    async def test_auto_login(request: Request):
        u, s = require(request)
        await form(request, s)
        if not (settings.auto_login and u["auto_login"] and u["swa_creds"]):
            raise Redirect("/settings?err=" + quote_plus("Save your details and turn automatic sign-in on first.") + "#auto")
        attempts = u["auto_login_attempts"] if u["auto_login_day"] == db.today() else 0
        if attempts >= app.state.worker.MANUAL_ATTEMPTS_PER_DAY:
            raise Redirect("/settings?err=" + quote_plus("No sign-in attempts left today. Try again tomorrow.") + "#auto")
        app.state.worker.enqueue_signin(u["id"])
        raise Redirect("/settings?ok=" + quote_plus("Sign-in check queued. Refresh in a minute or two.") + "#auto")

    @app.get("/verify", response_class=HTMLResponse)
    def verify_page(request: Request):
        u, s = require(request)
        return render(request, "verify.html", u, s)

    @app.post("/verify/{rid}")
    async def verify_submit(request: Request, rid: int):
        u, s = require(request)
        f = await form(request, s)
        code = re.sub(r"\s+", "", str(f.get("code", "")))
        req = db.one("SELECT * FROM code_requests WHERE id=? AND user_id=?", (rid, u["id"]))
        if not req or req["status"] != "waiting" or req["expires_at"] <= db.now():
            raise Redirect("/verify?err=" + quote_plus("That request has expired. The next sign-in will ask again."))
        if not re.fullmatch(r"[A-Za-z0-9]{4,10}", code):
            raise Redirect("/verify?err=" + quote_plus("Enter the code exactly as Southwest sent it."))
        with db.tx() as con:
            con.execute("UPDATE code_requests SET code=?, status='submitted' WHERE id=? AND status='waiting'",
                        (code, rid))
        raise Redirect("/settings?ok=" + quote_plus("Code sent to the sign-in. Check the status below in a minute.") + "#auto")

    @app.post("/settings")
    async def save_settings(request: Request):
        u, s = require(request)
        f = await form(request, s)
        email = str(f.get("email", "")).strip()
        if email and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
            raise Redirect("/settings?err=Enter+a+valid+email+address")
        mode = str(f.get("alert_mode", "instant"))
        mode = mode if mode in ("instant", "daily", "weekly") else "instant"
        with db.tx() as con:
            con.execute("UPDATE users SET email=?, alert_mode=? WHERE id=?", (email, mode, u["id"]))
        raise Redirect("/settings?ok=Saved")

    @app.post("/settings/swa")
    async def upload_swa(request: Request):
        u, s = require(request)
        f = await form(request, s)
        raw = str(f.get("state_text", "")).strip()
        up = f.get("state_file")
        if up is not None and hasattr(up, "read"):
            data = await up.read()
            if len(data) > 1_000_000:
                raise Redirect("/settings?err=File+too+large")
            if data.strip():
                raw = data.decode("utf-8", "replace")
        if not raw:
            raise Redirect("/settings?err=Choose+a+file+or+paste+the+JSON")
        try:
            state = swa.normalize_state(raw)
        except json.JSONDecodeError:
            raise Redirect("/settings?err=" + quote_plus("That isn't valid JSON."))
        except ValueError as e:
            raise Redirect("/settings?err=" + quote_plus(str(e)))
        except (KeyError, TypeError):
            raise Redirect("/settings?err=" + quote_plus("Unrecognized session file format."))
        with db.tx() as con:
            con.execute("UPDATE users SET swa_state=?, swa_state_updated=?, swa_status=?, swa_status_at=?"
                        " WHERE id=?", (encrypt_json(state), db.now(), "Uploaded, not used yet", db.now(), u["id"]))
        raise Redirect(f"/settings?ok=Southwest+session+saved+({len(state['cookies'])}+cookies)")

    @app.post("/settings/swa/clear")
    async def clear_swa(request: Request):
        u, s = require(request)
        await form(request, s)
        with db.tx() as con:
            con.execute("UPDATE users SET swa_state=NULL, swa_state_updated=NULL, swa_status=NULL WHERE id=?",
                        (u["id"],))
        swa.wipe_profile(u["id"])  # also forget the browser's own Southwest cookies
        raise Redirect("/settings?ok=Southwest+session+removed")

    # ---------- admin ----------
    def user_or_404(uid):
        t = db.one("SELECT * FROM users WHERE id=?", (uid,))
        if not t:
            raise Redirect("/admin?err=User+not+found")
        return t

    def parse_limits(f):
        errs, out = [], {}
        for k in ("max_watches", "max_searches_per_day"):
            v = str(f.get(k, "")).strip()
            if v == "":
                out[k] = None
            elif v.isdigit():
                out[k] = int(v)
            else:
                errs.append(f"{k.replace('_', ' ').capitalize()} must be a whole number or blank.")
        exp = str(f.get("expires_at", "")).strip()
        if exp:
            try:
                exp = dt.date.fromisoformat(exp).isoformat()
            except ValueError:
                errs.append("Expiry must be a date.")
        out["expires_at"] = exp or None
        out["is_admin"] = 1 if f.get("is_admin") else 0
        out["email"] = str(f.get("email", "")).strip()
        return out, errs

    @app.get("/admin", response_class=HTMLResponse)
    def admin(request: Request):
        u, s = require(request, admin=True)
        users = db.query(
            "SELECT u.*, (SELECT COUNT(*) FROM watches w WHERE w.user_id=u.id AND w.active=1) AS n_watches,"
            " (SELECT searches FROM usage g WHERE g.user_id=u.id AND g.day=?) AS today"
            " FROM users u ORDER BY u.active DESC, u.username", (db.today(),))
        from . import tzcheck
        return render(request, "admin_users.html", u, s, users=users, today=db.today(),
                      worker=app.state.worker, tz=tzcheck.last_result)

    @app.get("/admin/users/new", response_class=HTMLResponse)
    def admin_new(request: Request):
        u, s = require(request, admin=True)
        t = {"username": "", "email": "", "is_admin": 0, "max_watches": settings.default_max_watches,
             "max_searches_per_day": settings.default_max_searches, "expires_at": None}
        return render(request, "admin_user_form.html", u, s, t=t, new=True)

    @app.post("/admin/users")
    async def admin_create(request: Request):
        u, s = require(request, admin=True)
        f = await form(request, s)
        lim, errs = parse_limits(f)
        username = str(f.get("username", "")).strip()
        if not re.match(r"^[A-Za-z0-9._-]{3,32}$", username):
            errs.append("Username must be 3–32 letters, digits, dots, dashes or underscores.")
        elif db.one("SELECT 1 FROM users WHERE username=?", (username,)):
            errs.append("That username is taken.")
        if errs:
            return render(request, "admin_user_form.html", u, s, t={**lim, "username": username}, new=True,
                          err="; ".join(errs), status=400)
        pw = temp_password()
        with db.tx() as con:
            uid = con.execute(
                "INSERT INTO users(username, email, pw_hash, is_admin, must_change_pw, max_watches,"
                " max_searches_per_day, expires_at, created_at) VALUES (?,?,?,?,1,?,?,?,?)",
                (username, lim["email"], hash_password(pw), lim["is_admin"], lim["max_watches"],
                 lim["max_searches_per_day"], lim["expires_at"], db.now())).lastrowid
        return render(request, "temp_password.html", u, s, t=user_or_404(uid), password=pw, created=True)

    @app.get("/admin/users/{uid}", response_class=HTMLResponse)
    def admin_edit(request: Request, uid: int):
        u, s = require(request, admin=True)
        t = user_or_404(uid)
        watches = db.query("SELECT * FROM watches WHERE user_id=? ORDER BY active DESC, id", (uid,))
        return render(request, "admin_user_form.html", u, s, t=t, new=False, watches=watches,
                      today_used=db.searches_today(uid),
                      est={w["id"]: swa.searches_per_run(w) for w in watches})

    @app.post("/admin/users/{uid}")
    async def admin_update(request: Request, uid: int):
        u, s = require(request, admin=True)
        t = user_or_404(uid)
        f = await form(request, s)
        lim, errs = parse_limits(f)
        if uid == u["id"] and not lim["is_admin"]:
            errs.append("You can't remove your own admin role.")
        if errs:
            return render(request, "admin_user_form.html", u, s, t={**dict(t), **lim}, new=False,
                          err="; ".join(errs), status=400, watches=[], est={}, today_used=0)
        with db.tx() as con:
            con.execute("UPDATE users SET email=?, is_admin=?, max_watches=?, max_searches_per_day=?, expires_at=?"
                        " WHERE id=?", (lim["email"], lim["is_admin"], lim["max_watches"],
                                        lim["max_searches_per_day"], lim["expires_at"], uid))
        raise Redirect(f"/admin/users/{uid}?ok=Saved")

    async def _admin_action(request, uid):
        u, s = require(request, admin=True)
        t = user_or_404(uid)
        await form(request, s)
        return u, s, t

    @app.post("/admin/users/{uid}/revoke")
    async def admin_revoke(request: Request, uid: int):
        u, s, t = await _admin_action(request, uid)
        if uid == u["id"]:
            raise Redirect("/admin?err=You+can't+revoke+yourself")
        with db.tx() as con:
            con.execute("UPDATE users SET active=0 WHERE id=?", (uid,))
            con.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
        raise Redirect(f"/admin?ok=Revoked+{t['username']}")

    @app.post("/admin/users/{uid}/restore")
    async def admin_restore(request: Request, uid: int):
        u, s, t = await _admin_action(request, uid)
        with db.tx() as con:
            con.execute("UPDATE users SET active=1 WHERE id=?", (uid,))
        raise Redirect(f"/admin?ok=Restored+{t['username']}")

    @app.post("/admin/users/{uid}/reset-password")
    async def admin_reset(request: Request, uid: int):
        u, s, t = await _admin_action(request, uid)
        pw = temp_password()
        with db.tx() as con:
            con.execute("UPDATE users SET pw_hash=?, must_change_pw=1 WHERE id=?", (hash_password(pw), uid))
            con.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
        return render(request, "temp_password.html", u, s, t=t, password=pw, created=False)

    @app.post("/admin/users/{uid}/delete")
    async def admin_delete(request: Request, uid: int):
        u, s, t = await _admin_action(request, uid)
        if uid == u["id"]:
            raise Redirect("/admin?err=You+can't+delete+yourself")
        with db.tx() as con:
            con.execute("DELETE FROM users WHERE id=?", (uid,))
        swa.wipe_profile(uid)
        raise Redirect(f"/admin?ok=Deleted+{t['username']}")

    @app.post("/admin/run-all")
    async def admin_run_all(request: Request):
        u, s = require(request, admin=True)
        await form(request, s)
        n = app.state.worker.enqueue_all()
        raise Redirect(f"/admin?ok=Queued+{n}+user(s)")

    return app
