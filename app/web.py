"""FastAPI web app: login, user watches, Southwest session upload, admin panel."""
import datetime as dt
import hmac
import json
import re
import sys
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote_plus

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import db, swa
from .config import settings
from .mailer import fmt_dur
from .security import (encrypt_json, hash_password, new_token, temp_password, token_hash,
                       verify_password)
from .worker import worker as default_worker

HERE = Path(__file__).parent
COOKIE = "swa_session"
templates = Jinja2Templates(directory=str(HERE / "templates"))
templates.env.filters["dur"] = lambda m: fmt_dur(int(m or 0))
templates.env.filters["money"] = lambda v: "—" if v is None else f"${v:,.0f}"
templates.env.filters["pts"] = lambda v: "—" if v is None else f"{int(v):,}"
templates.env.filters["when"] = lambda s: (s or "")[:16].replace("T", " ") or "—"
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
        print(f"[setup] Created admin user '{settings.admin_username}' with temporary password: {pw}",
              file=sys.stderr, flush=True)


def create_app(worker=None, start_worker=True):
    worker = worker or default_worker

    @asynccontextmanager
    async def lifespan(app):
        db.init()
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

    def render(request, name, user=None, sess=None, status=200, **ctx):
        ctx.update(user=user, csrf=sess["csrf"] if sess else "", today_str=db.today(),
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

    # ---------- public ----------
    @app.get("/health")
    def health():
        db.one("SELECT 1")
        return JSONResponse({"status": "ok"})

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
            "origin": str(f.get("origin", "")).strip().upper(),
            "destination": str(f.get("destination", "")).strip().upper(),
            "dates": None, "days_from": None, "days_to": None,
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
            w["name"] = f'{w["origin"]}→{w["destination"]}'
        for k in ("origin", "destination"):
            if not IATA.match(w[k]):
                errs.append(f"{k.capitalize()} must be a 3-letter airport code.")
        if w["origin"] == w["destination"]:
            errs.append("Origin and destination must differ.")
        for k in ("depart_after", "depart_before"):
            if w[k] and not TIME.match(w[k]):
                errs.append("Departure times must be HH:MM.")
        if f.get("date_mode") == "window":
            w["days_from"] = opt_int("days_from", 0, 365)
            w["days_to"] = opt_int("days_to", 0, 365)
            if w["days_from"] is None or w["days_to"] is None or w["days_to"] < w["days_from"]:
                errs.append("Rolling window needs 'from' and 'to' days, with 'to' ≥ 'from'.")
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
                w["dates"] = ",".join(d.isoformat() for d in parsed) or None
        if w["max_price"] is None and w["max_points"] is None:
            errs.append("Set a max cash price, a max points price, or both.")
        return w, errs

    def watch_ctx(w):
        return {"w": w, "weekdays": WEEKDAY_LABELS,
                "estimate": swa.searches_per_run(w) if (w.get("dates") or w.get("days_to") is not None) else 0}

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        u, s = require(request)
        watches = db.query("SELECT * FROM watches WHERE user_id=? ORDER BY active DESC, id", (u["id"],))
        best = {}
        for w in watches:
            best[w["id"]] = db.one(
                "SELECT MIN(CASE WHEN matched=1 THEN cash END) AS cash,"
                " MIN(CASE WHEN matched=1 THEN points END) AS points,"
                " MIN(cash) AS any_cash, COUNT(*) AS n FROM fares"
                " WHERE watch_id=? AND ts=(SELECT MAX(ts) FROM fares WHERE watch_id=?)", (w["id"], w["id"]))
        return render(request, "dashboard.html", u, s, watches=watches, best=best,
                      est={w["id"]: swa.searches_per_run(w) for w in watches},
                      limits=limits_for(u), run_status=app.state.worker.status(u["id"]),
                      daily_at=settings.daily_at)

    @app.get("/watches/new", response_class=HTMLResponse)
    def new_watch(request: Request):
        u, s = require(request)
        w = {"name": "", "origin": "", "destination": "", "dates": "", "days_from": 14, "days_to": 45,
             "weekdays": "", "adults": 1, "max_stops": 1, "max_duration_min": None, "depart_after": "",
             "depart_before": "", "max_price": None, "max_points": None, "min_cpp": 1.3, "active": 1}
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
        raise Redirect(f"/watches/{wid}?ok=Watch+created")

    @app.get("/watches/{wid}", response_class=HTMLResponse)
    def watch_detail(request: Request, wid: int):
        u, s = require(request)
        w = watch_or_404(u, wid)
        last = db.one("SELECT MAX(ts) AS ts FROM fares WHERE watch_id=?", (wid,))["ts"]
        # latest results per date (dates can be searched in different runs)
        fares = db.query(
            "SELECT f.* FROM fares f JOIN (SELECT date, MAX(ts) ts FROM fares WHERE watch_id=? GROUP BY date) l"
            " ON f.date=l.date AND f.ts=l.ts WHERE f.watch_id=? ORDER BY f.date, f.matched DESC, f.cash",
            (wid, wid))
        trend = db.query(
            "SELECT substr(ts,1,10) AS day, MIN(CASE WHEN matched=1 THEN cash END) AS cash,"
            " MIN(CASE WHEN matched=1 THEN points END) AS points, MIN(cash) AS any_cash"
            " FROM fares WHERE watch_id=? GROUP BY day ORDER BY day DESC LIMIT 30", (wid,))
        runs = db.query("SELECT * FROM runs WHERE watch_id=? ORDER BY id DESC LIMIT 10", (wid,))
        return render(request, "watch_detail.html", u, s, w=w, fares=fares, last=last, trend=trend,
                      runs=runs, dates=swa.watch_dates(w), run_status=app.state.worker.status(u["id"]),
                      book=lambda d: swa.booking_url(w["origin"], w["destination"], d, w["adults"]),
                      recommend=lambda r: swa.recommend(r, w))

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

    # ---------- settings ----------
    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request):
        u, s = require(request)
        return render(request, "settings.html", u, s, limits=limits_for(u))

    @app.post("/settings")
    async def save_settings(request: Request):
        u, s = require(request)
        f = await form(request, s)
        email = str(f.get("email", "")).strip()
        if email and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
            raise Redirect("/settings?err=Enter+a+valid+email+address")
        with db.tx() as con:
            con.execute("UPDATE users SET email=? WHERE id=?", (email, u["id"]))
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
        return render(request, "admin_users.html", u, s, users=users, today=db.today(),
                      worker=app.state.worker)

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
        raise Redirect(f"/admin?ok=Deleted+{t['username']}")

    @app.post("/admin/run-all")
    async def admin_run_all(request: Request):
        u, s = require(request, admin=True)
        await form(request, s)
        n = app.state.worker.enqueue_all()
        raise Redirect(f"/admin?ok=Queued+{n}+user(s)")

    return app
