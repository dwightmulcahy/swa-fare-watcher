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
SEATS_LINK = 'a[href*="/air/seat/select-seats"]'
SEAT_BUTTON = 'button[aria-label^="Seat "]'
PASSENGER = '[role=radio][aria-label*="Seat:"]'
FLIGHT_TAB = '[role=tab][aria-label^="Flight "]'
SEATS_TOTAL = '[aria-label="Seat pricing summary"]'
CONTINUE = 'button:has-text("Continue to payment")'
CONFIRM = "#confirmSeatChange"


class NotFree(Exception):
    """The checkout asked for money: nothing was confirmed."""


class SeatPicker:
    def __init__(self, browser, conf, first, last, prefs, status, user_id=None):
        self.b, self.page = browser, browser.page
        self.conf, self.first, self.last = conf, first, last
        self.prefs, self.status, self.uid = prefs, status, user_id
        self.rng = browser.human.rng if browser.human else random

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
        return [s for s in map(seats.parse_seat, labels) if s]

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
        out = []
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
                new = seats.choose(smap, self.prefs, self.status, taken)
                if seats.better(new, cur, self.prefs):
                    out.append((fi, pi, cur_code, new))
                    taken.add(new["seat"])
                elif cur_code:
                    taken.add(cur_code)
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
            got = self.current_seat(self.passengers()[pi].get_attribute("aria-label"))
            if got != new["seat"]:
                raise RuntimeError(f"seat {new['seat']} didn't stick (shows {got})")
        total = self.page.locator(SEATS_TOTAL).first.inner_text(timeout=10_000)
        if money(total) != 0:
            raise NotFree(f"the seat map's total is {total.strip()}")

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
        (selection opening) or `until` (epoch seconds). Returns (changes, note)."""
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


def money(text):
    m = re.search(r"([\d,]+(?:\.\d+)?)", text or "")
    return float(m.group(1).replace(",", "")) if m else None
