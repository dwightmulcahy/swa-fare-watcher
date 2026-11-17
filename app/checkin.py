"""Check in to a booked Southwest flight when check-in opens (24 hours before departure).

The flow a person clicks through: the Check In page (confirmation # + first and last name) ->
"Check in" on the review page -> the confirmation page with each passenger's boarding group or
position. Nothing here buys anything: if the page offers only paid extras, it never clicks them.
"""
import random
import re
import time
from urllib.parse import urlencode

from . import applog

CHECKIN_URL = "https://www.southwest.com/air/check-in/index.html"
OPEN_HOURS = 24
FIELDS = {   # Southwest's check-in form; the manage-reservation ids as a fallback
    "conf": '[id="confirmationNumber"], [name="confirmationNumber"], [id$="record_locator"]',
    "first": '[id="passengerFirstName"], [name="passengerFirstName"], [id$="first_name"]',
    "last": '[id="passengerLastName"], [name="passengerLastName"], [id$="last_name"]',
}
SUBMIT = '#form-mixin--submit-button, button[type="submit"]:has-text("Check in"), #lookupReservation'
CHECKIN_BUTTON = 'button:has-text("Check in"):visible, [role="button"]:has-text("Check in"):visible'
TOO_EARLY = re.compile(r"too early|not (?:yet )?(?:available|eligible) for check[- ]?in|"
                       r"check[- ]?in (?:is )?(?:available|opens) (?:starting )?24 hours", re.I)
DONE = re.compile(r"you(?:'|’)re (?:all )?checked in|checked in!|boarding pass", re.I)
POSITION = re.compile(r"\bGroup\s*([1-9])\b|\b(?:Position|Boarding)\b[^A-Za-z0-9]{0,20}([ABC])\s?([1-9]\d?)\b", re.I)
SEAT = re.compile(r"\bSeat\s*(\d{1,2}[A-F])\b")


class TooEarly(Exception):
    """Southwest says check-in hasn't opened yet."""


def checkin_url(conf, first, last):
    """The Check In page with the form filled in (the query Southwest's own emails use)."""
    if not (conf and first and last):
        return CHECKIN_URL
    return CHECKIN_URL + "?" + urlencode({"confirmationNumber": conf, "passengerFirstName": first,
                                          "passengerLastName": last})


def boarding(text):
    """'Group 3', 'A23', ... and seats found on the confirmation page, in page order."""
    out = []
    for m in POSITION.finditer(text or ""):
        p = f"Group {m.group(1)}" if m.group(1) else f"{m.group(2).upper()}{m.group(3)}"
        if p not in out:
            out.append(p)
    seats = list(dict.fromkeys(SEAT.findall(text or "")))
    return out, seats


class CheckIn:
    def __init__(self, browser, conf, first, last, user_id=None):
        self.b, self.page = browser, browser.page
        self.conf, self.first, self.last, self.uid = conf, first, last, user_id
        self.rng = browser.human.rng if browser.human else random

    def log(self, msg, level="info"):
        getattr(applog, level)(msg, source="checkin", user_id=self.uid)

    def body(self):
        return self.page.inner_text("body")

    def open_form(self):
        resp = self.page.goto(CHECKIN_URL, wait_until="domcontentloaded", timeout=60_000)
        self.b.check_blocked("check-in", resp)
        self.b._dismiss_cookie_banner()
        self.page.locator(FIELDS["conf"]).first.wait_for(state="visible", timeout=30_000)

    def submit(self):
        """Fill the form like a person and wait for the review page (or a too-early message)."""
        for key, value in (("conf", self.conf), ("first", self.first), ("last", self.last)):
            el = self.page.locator(FIELDS[key]).first
            self.b._click(el)
            el.fill("")
            self.b._type(el, value)
            self.b._beat(0.3, 0.9)
        self.b._click(self.page.locator(SUBMIT).first)
        end = time.monotonic() + 45
        while time.monotonic() < end:
            self.b._beat(0.8, 1.4)
            self.b.check_blocked("check-in")
            text = self.body()
            if TOO_EARLY.search(text):
                raise TooEarly(TOO_EARLY.search(text).group(0))
            if DONE.search(text) and POSITION.search(text):
                return "done"
            if not self.page.locator(FIELDS["conf"]).count() and self.page.locator(CHECKIN_BUTTON).count():
                return "review"     # the form is gone and a Check in button is shown
        raise RuntimeError("the check-in page didn't move on after submitting the form")

    def confirm(self):
        self.b._click(self.page.locator(CHECKIN_BUTTON).first)
        end = time.monotonic() + 45
        while time.monotonic() < end:
            self.b._beat(0.8, 1.4)
            self.b.check_blocked("check-in")
            text = self.body()
            if DONE.search(text) and POSITION.search(text):
                return text
        raise RuntimeError("no boarding position shown after pressing Check in")

    def run(self, until, poll=(40, 70), sleep=time.sleep):
        """Check in, retrying while Southwest says it's too early, until `until` (epoch seconds).
        Returns (positions, seats)."""
        self.b.warm_up()
        while True:
            self.open_form()
            try:
                state = self.submit()
            except TooEarly as e:
                if time.time() >= until:
                    raise
                self.log(f"Check-in not open yet ({e}); trying again shortly")
                sleep(self.rng.uniform(*poll))
                continue
            text = self.body() if state == "done" else self.confirm()
            return boarding(text)
