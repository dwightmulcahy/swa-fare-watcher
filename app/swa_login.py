"""Sign in to southwest.com the way a person would, using the app's browser setup.

Used by `python -m app login-test` (run it on your own machine to see if sign-in works),
and meant to back the app's automatic sign-in fallback later.

Southwest's page structure isn't documented and changes, so every element is found by
several strategies (accessible role/label first, then CSS), and each step is screenshotted.
"""
import datetime as dt
import re
import time
from urllib.parse import urlsplit

HOME_URL = "https://www.southwest.com/"

# ---------- what we look for (several strategies each; first visible match wins) ----------
LOGIN_ENTRY = [
    ("role", "button", r"^\s*log\s*in\s*$"),
    ("role", "link", r"^\s*log\s*in\s*$"),
    ("role", "button", r"log\s*in|sign\s*in"),
    ("role", "link", r"log\s*in|sign\s*in"),
    ("css", "[data-qa*='login' i], [data-test*='login' i], a[href*='login' i]"),
]
# Field lists only ever match real text inputs (see find(fields=True)): on Southwest the
# "Forgot login?" link sits right next to the username label and matched a loose label search.
USERNAME = [
    ("label", r"^\s*(?:account\s*number\s*or\s*username|username|account\s*number|"
              r"rapid\s*rewards\s*(?:#|number|account\s*#?)?)\s*\*?\s*$"),
    ("css", "input[name='username'], input#username, input[autocomplete='username'], "
            "input[name*='account' i], input[id*='user' i]"),
    ("label", r"account\s*number|username"),
    ("placeholder", r"account\s*number|username"),
]
PASSWORD = [
    ("css", "input[type='password']"),
    ("label", r"^\s*password\s*\*?\s*$"),
]
SUBMIT = [
    ("role", "button", r"^\s*log\s*in\s*$"),
    ("role", "button", r"log\s*in|sign\s*in|continue|submit"),
    ("css", "button[type='submit'], input[type='submit']"),
]
# Strict on purpose: the booking widget on the same page has a "promo code" field.
CODE_INPUT = [
    ("css", "input[autocomplete='one-time-code']"),
    ("label", r"verification|passcode|security code|one[- ]time|^\s*code\s*$"),
    ("placeholder", r"verification|passcode|security code|one[- ]time|^\s*code\s*$"),
    ("css", "input[name*='otp' i], input[id*='otp' i], input[name*='passcode' i], "
            "input[name*='verification' i], input[id*='verification' i]"),
]
CODE_SUBMIT = [
    ("role", "button", r"verify|submit|continue|log\s*in|confirm"),
    ("css", "button[type='submit']"),
]

# Page-text signals, checked in this order.
BLOCKED_TEXT = re.compile(r"access denied|request (?:was )?(?:unsuccessful|blocked)|pardon our interruption|"
                          r"unusual (?:activity|traffic)|are you a robot|verify you are (?:a )?human|"
                          r"too many (?:requests|attempts)", re.I)
# No bare "enter code": the home page says things like "enter code at checkout" for promos.
MFA_TEXT = re.compile(r"verification code|one[- ]time (?:code|passcode)|security code|"
                      r"we sent (?:a|you a) code|verify (?:it'?s|its) you|confirm (?:it'?s|its) you", re.I)
BAD_LOGIN_TEXT = re.compile(r"(?:username|account number|password)[^.]{0,60}(?:incorrect|invalid|"
                            r"doesn'?t match|do not match|not recognized)|incorrect (?:username|password)|"
                            r"unable to log ?in|account (?:is |has been )?locked", re.I)
# Only signals a signed-out page wouldn't show (marketing copy mentions points everywhere).
LOGGED_IN_TEXT = re.compile(r"\blog\s*out\b|\bsign\s*out\b|\bhi,\s+[A-Z][\w'-]+", re.I)
# Shown only while signed out (Southwest header: "Log in to view points balance").
SIGNED_OUT_TEXT = re.compile(r"log\s*in to view (?:your )?points", re.I)
# Header login button only (not the submit button inside the popup).
HEADER_LOGIN = [("role", "button", r"^\s*log\s*in\s*$"), ("role", "link", r"^\s*log\s*in\s*$")]

RESULTS = {
    "success": 0, "already_signed_in": 0,
    "mfa_required": 2, "bad_credentials": 3, "blocked": 4,
    "not_found": 5, "timeout": 5, "error": 5,
}


class LoginResult:
    def __init__(self, status, detail="", name=None):
        self.status, self.detail, self.name = status, detail, name

    @property
    def ok(self):
        return self.status in ("success", "already_signed_in")

    @property
    def exit_code(self):
        return RESULTS.get(self.status, 5)

    def __repr__(self):
        return f"LoginResult({self.status!r}, {self.detail!r})"


# ---------- element finding ----------
def _candidates(scope, strategy):
    kind = strategy[0]
    if kind == "role":
        return scope.get_by_role(strategy[1], name=re.compile(strategy[2], re.I))
    if kind == "label":
        return scope.get_by_label(re.compile(strategy[1], re.I))
    if kind == "placeholder":
        return scope.get_by_placeholder(re.compile(strategy[1], re.I))
    return scope.locator(strategy[1])


_IS_TEXT_FIELD = """e => (e.tagName === 'TEXTAREA' || (e.tagName === 'INPUT' &&
    !['hidden','checkbox','radio','submit','button','image','reset','file'].includes((e.type||'').toLowerCase())))
    && !e.readOnly"""


def find(page, strategies, timeout=0.0, poll=0.25, within=None, fields=False):
    """First visible element matching any strategy, searching the page and its iframes
    (or only inside `within`, a locator such as the login form). fields=True accepts only
    editable text inputs, never links or buttons that happen to share a label."""
    end = time.monotonic() + timeout
    while True:
        scopes = [within] if within is not None else [page, *[f for f in page.frames if f != page.main_frame]]
        for scope in scopes:
            for strat in strategies:
                try:
                    loc = _candidates(scope, strat)
                    for i in range(min(loc.count(), 5)):
                        el = loc.nth(i)
                        if el.is_visible() and el.is_enabled():
                            if fields and not el.evaluate(_IS_TEXT_FIELD):
                                continue
                            return el
                except Exception:
                    continue
        if time.monotonic() >= end:
            return None
        time.sleep(poll)


def container_of(el):
    """The form (or dialog) an input belongs to, or None."""
    for xp in ("xpath=ancestor::form[1]", "xpath=ancestor::*[@role='dialog' or @aria-modal='true'][1]"):
        try:
            c = el.locator(xp)
            if c.count():
                return c.first
        except Exception:
            pass
    return None


def page_text(page):
    parts = []
    for f in page.frames:
        try:
            parts.append(f.locator("body").inner_text(timeout=2000))
        except Exception:
            pass
    return "\n".join(parts)


def header_text(page):
    try:
        return page.locator("header").first.inner_text(timeout=1500)
    except Exception:
        return ""


def signed_in_signal(page, text):
    """Evidence of being signed in, or None. Southwest's header swaps the "Log in" button for
    the member's name/points, so a missing header button counts when the page has rendered."""
    m = LOGGED_IN_TEXT.search(text)
    if m:
        return m.group(0)
    rendered = len(text) > 400  # not a loading skeleton
    if (rendered and not SIGNED_OUT_TEXT.search(text) and find(page, HEADER_LOGIN) is None
            and find(page, PASSWORD, fields=True) is None):
        return "header shows account instead of Log in"
    return None


def classify(page):
    """Look at the current page and decide where sign-in stands."""
    text = page_text(page)
    title = ""
    try:
        title = page.title()
    except Exception:
        pass
    if BLOCKED_TEXT.search(text) or BLOCKED_TEXT.search(title):
        return "blocked", BLOCKED_TEXT.search(text + " " + title).group(0)
    if find(page, CODE_INPUT, fields=True) is not None or MFA_TEXT.search(text):
        m = MFA_TEXT.search(text)
        return "mfa_required", m.group(0) if m else "code field shown"
    if BAD_LOGIN_TEXT.search(text):
        return "bad_credentials", BAD_LOGIN_TEXT.search(text).group(0)
    sig = signed_in_signal(page, text)
    if sig:
        return "success", sig
    return None, ""


def account_name(page):
    text = page_text(page)
    m = re.search(r"\bhi,\s+([A-Z][\w'-]+)", text, re.I)
    if m:
        return m.group(1)
    m = re.search(r"\b([A-Z][a-z'-]+)\s*[\n|·•]?\s*[\d,]+\s*(?:pts|points)\b", header_text(page))
    return m.group(1) if m else None


# ---------- the flow ----------
class Recorder:
    """Numbered screenshots + a step log for a run."""

    def __init__(self, page, out_dir, log=print):
        self.page, self.dir, self.n, self._log = page, out_dir, 0, log
        self.dir.mkdir(parents=True, exist_ok=True)

    def log(self, msg):
        line = f"[{dt.datetime.now():%H:%M:%S}] {msg}"
        self._log(line)
        with open(self.dir / "steps.log", "a") as f:
            f.write(line + "\n")

    def shot(self, name):
        self.n += 1
        path = self.dir / f"{self.n:02d}-{name}.png"
        for attempt in range(3):  # a just-opened window may not have painted yet
            try:
                self.page.screenshot(path=str(path), full_page=False, timeout=10_000)
                break
            except Exception as e:
                if attempt == 2:
                    self.log(f"  (screenshot failed: {str(e).splitlines()[0][:150]})")
                time.sleep(1)
        return path


def watch_network(page, rec):
    """Log Southwest request paths + status codes (no bodies, no query strings: no secrets)."""
    def on_response(resp):
        try:
            u = urlsplit(resp.url)
            if "southwest.com" not in u.netloc or resp.request.resource_type not in ("xhr", "fetch", "document"):
                return
            if resp.request.method != "GET" or resp.status >= 400 or resp.request.resource_type == "document":
                with open(rec.dir / "network.log", "a") as f:
                    f.write(f"{dt.datetime.now():%H:%M:%S} {resp.status} {resp.request.method} {u.netloc}{u.path}\n")
        except Exception:
            pass
    page.on("response", on_response)


def wait_outcome(page, timeout=30.0):
    """Poll until the same result is seen twice in a row (pages re-render while loading)."""
    end = time.monotonic() + timeout
    last = None
    while time.monotonic() < end:
        status, detail = classify(page)
        if status and status == last:
            return status, detail
        last = status
        time.sleep(1.0)
    return "timeout", f"no recognisable result after {timeout:.0f}s"


def _value(el):
    try:
        return el.input_value(timeout=2000)
    except Exception:
        return None


def type_checked(human, el, text, secret=False):
    """Type into a field and confirm it took. Retries once, slowly and without typos."""
    human.type(el, text, typos=not secret, clear=True)
    got = _value(el)
    ok = (got is not None and len(got) == len(text)) if secret else (got is not None and got.strip() == text)
    if ok:
        return True
    human.type(el, text, typos=False, clear=True)
    got = _value(el)
    return (got is not None and len(got) == len(text)) if secret else (got is not None and got.strip() == text)


def login(page, human, username, password, rec, code_provider=None, home_url=HOME_URL,
          manual_assist=None, remember_me=True):
    """Sign in. code_provider() -> str returns a verification code (or None to give up).
    manual_assist(msg) -> bool lets a watching person fix things in the window and continue."""
    rec.log(f"Opening {home_url}")
    page.goto(home_url, wait_until="domcontentloaded", timeout=60_000)
    try:
        page.wait_for_load_state("load", timeout=20_000)
    except Exception:
        pass
    status, detail = classify(page)
    if status == "blocked":
        rec.shot("blocked")
        return LoginResult("blocked", detail)
    human.read(human.rng.uniform(3, 6))
    rec.shot("home")

    if find(page, HEADER_LOGIN) is None and signed_in_signal(page, page_text(page)):
        rec.log("Already signed in (saved browser profile)")
        return LoginResult("already_signed_in", "profile still has a session", account_name(page))

    # 1. open the login form: the password box is the anchor, the form around it is the scope
    pw_el = find(page, PASSWORD, fields=True)
    if pw_el is None:
        entry = find(page, HEADER_LOGIN, timeout=10) or find(page, LOGIN_ENTRY, timeout=2)
        if entry is None:
            rec.shot("no-login-button")
            if not (manual_assist and manual_assist("Couldn't find the Log in button. Open the login form "
                                                    "in the browser window, then press Enter here.")):
                return LoginResult("not_found", "Log in button not found")
        else:
            rec.log("Clicking Log in")
            human.click(entry)
        pw_el = find(page, PASSWORD, fields=True, timeout=15)
    if pw_el is None:
        rec.shot("no-login-form")
        status, detail = classify(page)
        return LoginResult(status or "not_found", detail or "login form (password box) not found")
    form = container_of(pw_el)
    user_el = (find(page, USERNAME, fields=True, within=form) if form is not None else None) \
        or find(page, USERNAME, fields=True)
    if user_el is None:
        rec.shot("no-username-field")
        return LoginResult("not_found", "username box not found in the login form")
    rec.shot("login-form")
    start_url = page.url

    # 2. type credentials, checking each one landed
    rec.log("Typing username")
    if not type_checked(human, user_el, username):
        rec.shot("username-not-entered")
        return LoginResult("error", "username didn't end up in the username box")
    if page.url != start_url:
        rec.shot("navigated-away")
        return LoginResult("error", f"page changed while typing ({urlsplit(page.url).path})")
    rec.log("Typing password (no typos)")
    if not type_checked(human, pw_el, password, secret=True):
        rec.shot("password-not-entered")
        return LoginResult("error", "password didn't end up in the password box")

    if remember_me:
        # keeps the Southwest session alive longer, so fewer sign-ins are needed later
        try:
            box = form.get_by_role("checkbox", name=re.compile(r"remember\s*me", re.I)).first \
                if form is not None else None
            if box is not None and box.count() and not box.is_checked():
                target = box if box.is_visible() else form.get_by_text(re.compile(r"^\s*remember\s*me\s*$", re.I)).first
                rec.log("Ticking Remember me")
                human.click(target)
        except Exception:
            pass
    rec.shot("filled")

    # The header has its own "Log in" button, so only look for submit inside the login form.
    if _value(user_el) is None or not _value(user_el).strip():
        rec.shot("username-empty")
        return LoginResult("error", "username box is empty at submit time")
    submit = find(page, SUBMIT, timeout=3, within=form) if form is not None else None
    if submit is not None:
        rec.log("Submitting (clicking the form's Log in button)")
        human.click(submit)
    else:
        rec.log("Submitting (Enter in the password box)")
        human.press("Enter")

    # 3. wait for the result
    status, detail = wait_outcome(page)
    rec.shot(status)
    rec.log(f"Result: {status} ({detail})")

    # 4. verification code
    attempts = 0
    while status == "mfa_required" and attempts < 2:
        attempts += 1
        code_el = find(page, CODE_INPUT, fields=True, timeout=5)
        if code_el is None:
            # probably asking how to send the code (text/email): let a person choose
            if manual_assist and manual_assist("Southwest wants to verify it's you. Choose how to get "
                                               "the code in the browser window, then press Enter here."):
                code_el = find(page, CODE_INPUT, fields=True, timeout=30)
            if code_el is None:
                return LoginResult("mfa_required", detail)
        code = code_provider() if code_provider else None
        if not code:
            return LoginResult("mfa_required", detail)
        rec.log("Typing verification code")
        human.type(code_el, code.strip(), typos=False, clear=True)
        btn = find(page, CODE_SUBMIT, timeout=3, within=container_of(code_el))
        if btn is not None:
            human.click(btn)
        else:
            human.press("Enter")
        status, detail = wait_outcome(page)
        rec.shot(f"after-code-{status}")
        rec.log(f"Result: {status} ({detail})")

    if status == "success":
        human.read(human.rng.uniform(2, 4))
        rec.shot("signed-in")
        return LoginResult("success", detail, account_name(page))
    return LoginResult(status, detail)
