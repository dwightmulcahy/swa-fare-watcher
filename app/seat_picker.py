"""Pick seats on a booked Southwest flight when seat selection opens (48 h before departure).

The flow is the one a person clicks through, on the pages Southwest serves:
  Manage reservation lookup (confirmation # + name) -> trip page -> "Upgrade or modify seats"
  -> seat map (one tab per flight, one radio per passenger, a button per seat whose aria-label
  carries its type, position, price and availability) -> "Continue to payment" -> review page
  ("Amount due" and "Confirm seat change").
Nothing is ever paid for: seats must be priced $0 on the map, the map's total must be $0, and
the review page's amount due must read 0.00, or it stops before confirming.
"""
import random
import re
import time

from . import applog, seats

MANAGE_URL = "https://www.southwest.com/air/manage-reservation/"
LOOKUP = {
    "conf": '[id="reservationForm.record_locator"]',
    "first": '[id="reservationForm.first_name"]',
    "last": '[id="reservationForm.last_name"]',
    "submit": "#lookupReservation",
}
def manage_url(conf, first, last):
    """The manage-reservation page with the lookup form filled in (the same query Southwest's own
    emails use); the plain page if anything is missing."""
    from urllib.parse import urlencode
    base = "https://www.southwest.com/air/manage-reservation/index.html"
    if not (conf and first and last):
        return MANAGE_URL
    return base + "?" + urlencode({"confirmationNumber": conf, "passengerFirstName": first,
                                   "passengerLastName": last})


SEATS_LINK = 'a[href*="/air/seat/select-seats"]'
SEAT_BUTTON = 'button[aria-label^="Seat "]'
PASSENGER = '[role=radio][aria-label*="Seat:"]'
FLIGHT_TAB = '[role=tab][aria-label^="Flight "]'
SEATS_TOTAL = '[aria-label="Seat pricing summary"]'
CONTINUE = 'button:has-text("Continue to payment")'
# The seat details panel's Select, and Ok on its "Please Note! ... no refunds ..." notice: matched
# by role or exact text, since they needn't be <button>s
SELECT = ':is(button, a, [role=button]):text-is("Select"):visible'
NOTE = ':is([role=dialog], [role=alertdialog], [aria-modal=true], div):has-text("no refunds"):visible'
NOTE_OK = ':is(button, a, [role=button], input[type=button], span, div):text-is("Ok"):visible'
CONFIRM = "#confirmSeatChange"


class NotFree(Exception):
    """The checkout asked for money: nothing was confirmed."""


class SeatPicker:
    def __init__(self, browser, conf, first, last, prefs, status, user_id=None, window=True):
        self.b, self.page = browser, browser.page
        self.conf, self.first, self.last = conf, first, last
        self.prefs, self.status, self.uid = prefs, status, user_id
        self.window = window     # within 48 h of departure: the status's upgrade seats count too
        self.rng = browser.human.rng if browser.human else random
        self.report = []     # per flight and passenger: current seat and the best free seats, scored

    def log(self, msg, level="info", **kw):
        getattr(applog, level)(msg, source="seats", user_id=self.uid, **kw)

    # ----- getting there
    def open_reservation(self):
        """Look the booking up like a person and wait for its trip page."""
        page = self.page
        resp = page.goto(MANAGE_URL, wait_until="domcontentloaded", timeout=60_000)
        self.b.check_blocked("manage reservation", resp)
        self.b._dismiss_cookie_banner()
        page.locator(LOOKUP["conf"]).wait_for(state="visible", timeout=30_000)
        for key, value in (("conf", self.conf), ("first", self.first), ("last", self.last)):
            el = page.locator(LOOKUP[key]).first
            self.b._click(el)
            self.b._type(el, value)
            self.b._beat(0.3, 0.9)
        self.b._click(page.locator(LOOKUP["submit"]).first)
        page.locator(SEATS_LINK).first.wait_for(state="visible", timeout=45_000)
        self.b.check_blocked("trip page")
        if self.b.humanize:
            self.b.human.read()

    def open_seat_map(self):
        link = self.page.locator(f"{SEATS_LINK}:visible").first
        self.b._click(link)
        self.page.locator(SEAT_BUTTON).first.wait_for(state="attached", timeout=45_000)
        self.b.check_blocked("seat map")
        self.b._beat(1.0, 2.5)

    def reload_seat_map(self):
        self.page.reload(wait_until="domcontentloaded", timeout=60_000)
        self.page.locator(SEAT_BUTTON).first.wait_for(state="attached", timeout=45_000)
        self.b.check_blocked("seat map")
        self.b._beat(0.8, 2.0)

    # ----- reading the map
    def seat_map(self):
        labels = self.page.eval_on_selector_all(SEAT_BUTTON, "els => els.map(e => e.getAttribute('aria-label'))")
        return seats.annotate([s for s in map(seats.parse_seat, labels) if s])

    def passengers(self):
        return self.page.locator(PASSENGER).all()

    @staticmethod
    def current_seat(label):
        m = re.search(r"Seat:\s*(\d+[A-K])", label or "")
        return m.group(1) if m else None

    def flights(self):
        tabs = self.page.locator(FLIGHT_TAB).all()
        return tabs or [None]

    # ----- choosing
    def plan(self):
        """[(flight index, passenger index, current seat, new seat dict)] for every passenger on
        every flight shown where a better free seat is available now. Clicks only tabs and
        passengers (no seat yet)."""
        out, report = [], []
        for fi, tab in enumerate(self.flights()):
            if tab is not None and tab.get_attribute("aria-selected") != "true":
                self.b._click(tab)
                self.b._beat(0.8, 1.8)
            taken = set()
            for pi, pax in enumerate(self.passengers()):
                if pax.get_attribute("aria-checked") != "true":
                    self.b._click(pax)
                    self.b._beat(0.4, 1.0)
                smap = self.seat_map()
                cur_code = self.current_seat(pax.get_attribute("aria-label"))
                cur = next((s for s in smap if s["seat"] == cur_code), None)
                options = seats.ranked(smap, self.prefs, self.status, taken, self.window)
                new = options[0] if options else None
                report.append({
                    "flight": (tab.get_attribute("aria-label") or "").split(" hours")[0] if tab is not None else "",
                    "passenger": (pax.get_attribute("aria-label") or "").split(" Seat:")[0],
                    "current": _scored(cur, self.prefs),
                    "options": [_scored(x, self.prefs) for x in options[:6]],
                    "picked": new["seat"] if seats.better(new, cur, self.prefs) else None})
                if seats.better(new, cur, self.prefs):
                    out.append((fi, pi, cur_code, new))
                    taken.add(new["seat"])
                elif cur_code:
                    taken.add(cur_code)
        self.report = report
        return out

    def apply(self, plan):
        """Click the planned seats, then check the map's total is $0."""
        tabs = self.flights()
        for fi, pi, _, new in plan:
            tab = tabs[fi]
            if tab is not None and tab.get_attribute("aria-selected") != "true":
                self.b._click(tab)
                self.b._beat(0.8, 1.8)
            pax = self.passengers()[pi]
            if pax.get_attribute("aria-checked") != "true":
                self.b._click(pax)
                self.b._beat(0.4, 1.0)
            btn = self.page.locator(f'{SEAT_BUTTON}[aria-label^="Seat {new["seat"]} "]').first
            self.b._click(btn)
            self.b._beat(0.8, 1.6)
            self.confirm_seat(pi, new["seat"])
        total = self.page.locator(SEATS_TOTAL).first.inner_text(timeout=10_000)
        if money(total) != 0:
            raise NotFree(f"the seat map's total is {total.strip()}")

    def _note_ok(self):
        """The Ok of the no-refund notice if it's showing (innermost match), else None."""
        if not re.search(r"no refunds", self.page.inner_text("body"), re.I):
            return None
        btn = self.page.get_by_role("button", name=re.compile(r"^\s*ok\s*$", re.I))
        if btn.count():
            return btn.last
        loc = self.page.locator(NOTE_OK)
        return loc.last if loc.count() else None

    def confirm_seat(self, pi, code, timeout=15):
        """A seat click opens its details panel: press Select, then Ok on the "no refunds if you
        paid for your original seat" notice, until the passenger shows the seat. Never presses
        Select while the bar's seats total isn't $0, and never again while the notice is up."""
        end = time.monotonic() + timeout
        oks = 0
        while True:
            got = self.current_seat(self.passengers()[pi].get_attribute("aria-label"))
            if got == code:
                return
            ok = self._note_ok()
            if ok is not None:
                oks += 1
                if oks > 1 and self.page.keyboard:            # a click that didn't take: try the keyboard
                    ok.focus()
                    self.page.keyboard.press("Enter")
                else:
                    self.b._click(ok)
                self.b._beat(1.0, 1.8)
            elif re.search(r"no refunds", self.page.inner_text("body"), re.I):
                raise RuntimeError(f"seat {code}: the 'no refunds' notice has no Ok button I can find")
            elif self.page.locator(SELECT).count():
                total = self.page.locator(SEATS_TOTAL)
                text = total.first.inner_text(timeout=5_000) if total.count() else self.page.inner_text("body")
                m = re.search(r"Seats total:\s*\$\s*([\d,]+(?:\.\d+)?)", text)
                if m and money(m.group(1)) != 0:
                    raise NotFree(f"seat {code} would cost ${m.group(1)}")
                self.b._click(self.page.locator(SELECT).last)
                self.b._beat(0.8, 1.4)
            else:
                self.b._beat(0.5, 1.0)
            if time.monotonic() > end:
                raise RuntimeError(f"seat {code} didn't stick (shows {got})")

    def checkout(self):
        """Continue to the review page and confirm only if the amount due is 0.00."""
        self.b._click(self.page.locator(CONTINUE).first)
        confirm = self.page.locator(CONFIRM).first
        confirm.wait_for(state="visible", timeout=45_000)
        self.b.check_blocked("seat review")
        body = self.page.inner_text("body")
        due = re.search(r"Amount due.*?\$\s*([\d,]+\.\d{2})", body, re.S)
        if not due or money(due.group(1)) != 0:
            raise NotFree(f"the review page shows amount due {due.group(1) if due else 'unknown'}")
        if self.b.humanize:
            self.b.human.pause(1.0, 2.5)
        before = self.page.url
        self.b._click(confirm)
        try:
            self.page.wait_for_url(lambda u: u != before, timeout=45_000)
        except Exception:
            confirm.wait_for(state="detached", timeout=15_000)

    def verify(self, codes):
        """Look the booking up again and check each new seat is on it."""
        self.open_reservation()
        text = self.page.inner_text("body")
        return [c for c in codes if not re.search(rf"\bSeat {c}\b", text)]

    # ----- the whole job
    def run(self, until, poll=(45, 90), sleep=time.sleep, stop=lambda: False):
        """Open the booking, then keep checking the seat map until a better free seat shows up
        (selection opening) or `until` (epoch seconds). Returns (changes, note). A failure carries
        a screenshot of the page (e.screenshot) for the log."""
        try:
            return self._run(until, poll, sleep, stop)
        except Exception as e:
            if not getattr(e, "screenshot", None) and hasattr(self.b, "capture"):
                e.screenshot = self.b.capture("seat-error")
            self._save_page()
            raise

    def _save_page(self):
        """The page's HTML next to the screenshots (stays on this server), to see what a control
        that couldn't be found really is."""
        try:
            import datetime as dt

            from .config import settings
            d = settings.debug_dir / "pages"
            d.mkdir(parents=True, exist_ok=True)
            p = d / f"{dt.datetime.now():%Y%m%d-%H%M%S}-seat-error.html"
            p.write_text(self.page.content())
            self.log(f"Seat map page saved for debugging: {p}", "warn")
        except Exception:
            pass

    def _run(self, until, poll, sleep, stop):
        self.b.warm_up()
        self.open_reservation()
        self.open_seat_map()
        while True:
            plan = self.plan()
            if plan:
                self.apply(plan)
                self.checkout()
                missing = self.verify([new["seat"] for *_, new in plan])
                changes = [(cur, seats.describe(new)) for _, _, cur, new in plan]
                if missing:
                    return changes, f"confirmed, but the trip page doesn't show seat(s) {', '.join(missing)} yet"
                return changes, "confirmed"
            if time.time() >= until or stop():
                return [], "no better free seat was offered"
            sleep(self.rng.uniform(*poll))
            self.reload_seat_map()


def parse_trip(text):
    """Flights on a trip page's text: [{"leg", "date", "time", "from", "to", "flights"}].
    Each trip block starts "Departing"/"Returning" + "Sat, November 14, 2026" and lists
    "Flight / WN567", "Departs / <city> / 1:15 / PM / <city>, <region> - LIR" and "Arrives ... - HOU"
    (a connection has several; the first departure and last arrival count)."""
    import datetime as dt
    lines = [x.strip() for x in (text or "").splitlines() if x.strip()]
    legs, cur = [], None
    for i, line in enumerate(lines):
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if line in ("Departing", "Returning"):
            try:
                day = dt.datetime.strptime(nxt, "%a, %B %d, %Y").date()
            except ValueError:
                continue
            if len(legs) == 2:
                break
            cur = {"leg": "ret" if legs else "out", "date": day.isoformat(), "time": None, "from": None,
                   "to": None, "flights": []}
            legs.append(cur)
        elif cur is None:
            continue
        elif line == "Flight":
            m = re.match(r"WN\s*(\d+)", nxt)
            if m:
                cur["flights"].append(m.group(1))
        elif line in ("Departs", "Arrives"):
            window = lines[i + 1:i + 7]
            code = next((m.group(1) for x in window for m in [re.search(r" - ([A-Z]{3})$", x)] if m), None)
            if line == "Departs" and cur["from"] is None:
                cur["from"] = code
                for j, x in enumerate(window):
                    t = re.fullmatch(r"(\d{1,2}):(\d{2})\s*([AP]M)?", x)
                    if t:
                        ampm = t.group(3) or (window[j + 1] if j + 1 < len(window) else "")
                        h = int(t.group(1)) % 12 + (12 if ampm.upper() == "PM" else 0)
                        cur["time"] = f"{h:02d}:{t.group(2)}"
                        break
            elif line == "Arrives":
                cur["to"] = code
    out = []
    for leg in legs:
        if leg["from"] and leg["to"] and leg["time"]:
            out.append({**leg, "flights": "/".join(leg["flights"])})
    return out


def _scored(seat, prefs):
    if seat is None:
        return None
    return {"seat": seat["seat"], "describe": seats.describe(seat), "price": seat["price"],
            "score": round(seats.fit(seat, prefs), 1)}


def money(text):
    m = re.search(r"([\d,]+(?:\.\d+)?)", text or "")
    return float(m.group(1).replace(",", "")) if m else None
