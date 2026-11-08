"""Human-like mouse, keyboard and scrolling for Playwright pages.

Everything goes through page.mouse / page.keyboard, so the browser sees trusted
input events (mousemove, mousedown, keydown, input, wheel) with realistic timing,
not synthetic element.click() / fill() calls.
"""
import math
import random
import time

# Characters typed with Shift held on a US keyboard.
SHIFTED = set('~!@#$%^&*()_+{}|:"<>?ABCDEFGHIJKLMNOPQRSTUVWXYZ')

# Neighbouring keys on a US QWERTY keyboard, for realistic typos.
_ROWS = ["`1234567890-=", "qwertyuiop[]\\", "asdfghjkl;'", "zxcvbnm,./"]
_NEIGHBOURS = {}
for r, row in enumerate(_ROWS):
    for c, ch in enumerate(row):
        near = set()
        for dr in (-1, 0, 1):
            rr = r + dr
            if 0 <= rr < len(_ROWS):
                for dc in (-1, 0, 1):
                    cc = c + dc
                    if (dr or dc) and 0 <= cc < len(_ROWS[rr]):
                        near.add(_ROWS[rr][cc])
        _NEIGHBOURS[ch] = "".join(sorted(near & set("abcdefghijklmnopqrstuvwxyz0123456789")))


def neighbour_key(ch, rng):
    near = _NEIGHBOURS.get(ch.lower())
    if not near:
        return None
    wrong = rng.choice(near)
    return wrong.upper() if ch.isupper() else wrong


def _bezier(p0, p1, p2, p3, t):
    u = 1 - t
    return (u**3 * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t**3 * p3[0],
            u**3 * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t**3 * p3[1])


def _ease(t):
    # slow start, fast middle, slow finish (minimum-jerk profile)
    return t**3 * (10 - 15 * t + 6 * t * t)


def path(start, end, rng, steps=None):
    """Points along a curved, eased path from start to end with slight hand tremor."""
    (x0, y0), (x1, y1) = start, end
    dist = math.hypot(x1 - x0, y1 - y0)
    if dist < 1:
        return [end]
    steps = steps or max(8, min(80, int(dist / rng.uniform(9, 16))))
    # control points pushed off the straight line, same side most of the time
    nx, ny = -(y1 - y0) / dist, (x1 - x0) / dist
    bend = rng.uniform(0.08, 0.3) * dist * rng.choice((1, 1, 1, -1))
    c1 = (x0 + (x1 - x0) * rng.uniform(0.2, 0.4) + nx * bend * rng.uniform(0.6, 1.2),
          y0 + (y1 - y0) * rng.uniform(0.2, 0.4) + ny * bend * rng.uniform(0.6, 1.2))
    c2 = (x0 + (x1 - x0) * rng.uniform(0.6, 0.8) + nx * bend * rng.uniform(0.3, 0.9),
          y0 + (y1 - y0) * rng.uniform(0.6, 0.8) + ny * bend * rng.uniform(0.3, 0.9))
    pts = []
    for i in range(1, steps + 1):
        t = _ease(i / steps)
        x, y = _bezier((x0, y0), c1, c2, (x1, y1), t)
        if i < steps:  # hand tremor, independent on each axis
            x += rng.gauss(0, 0.6)
            y += rng.gauss(0, 0.6)
        pts.append((x, y))
    return pts


class Human:
    """One person at one browser window. Keeps a fixed scroll-wheel step (a physical wheel
    always moves the same amount per notch) and tracks where the pointer is."""

    WHEEL_NOTCH = 100  # Chrome on Windows/Linux reports 100 px per wheel notch at 100% zoom

    def __init__(self, page, rng=None, sleep=time.sleep, typo_rate=0.03, speed=1.0):
        self.page = page
        self.rng = rng or random.Random()
        self.sleep = sleep
        self.typo_rate = typo_rate
        self.speed = speed  # >1 = slower person
        vp = page.viewport_size
        if not vp:  # real window size (no_viewport contexts)
            try:
                vp = page.evaluate("() => ({width: innerWidth, height: innerHeight})")
            except Exception:
                vp = None
        vp = vp or {"width": 1366, "height": 768}
        self.w, self.h = vp["width"], vp["height"]
        # The pointer arrives from the browser's toolbar/tab strip, i.e. the top edge of the page.
        self.pos = (self.rng.uniform(0.15, 0.85) * self.w, self.rng.uniform(1, 12))

    # ---------- timing ----------
    def pause(self, lo, hi):
        self.sleep(self.rng.uniform(lo, hi) * self.speed)

    def _dwell(self):
        # how long a key is held down: median ~85 ms, like real typing (automation is ~2 ms)
        return min(max(self.rng.lognormvariate(math.log(0.085), 0.3), 0.035), 0.22) * self.speed

    def _key_delay(self, prev, ch):
        # gap after releasing one key before pressing the next; with the dwell this gives a
        # median ~160 ms key-to-key interval (~70 wpm), slower around word/punctuation boundaries
        d = self.rng.lognormvariate(math.log(0.075), 0.4)
        if prev == " " or ch == " ":
            d += self.rng.uniform(0.03, 0.15)
        if not ch.isalnum() and ch != " ":
            d += self.rng.uniform(0.05, 0.2)
        if self.rng.random() < 0.04:  # brief hesitation
            d += self.rng.uniform(0.3, 0.9)
        return min(d, 1.6) * self.speed

    # ---------- mouse ----------
    def move_to(self, x, y, overshoot=True):
        x = min(max(x, 1), self.w - 2)
        y = min(max(y, 1), self.h - 2)
        dist = math.hypot(x - self.pos[0], y - self.pos[1])
        if overshoot and dist > 250 and self.rng.random() < 0.3:
            ang = math.atan2(y - self.pos[1], x - self.pos[0])
            over = self.rng.uniform(8, 25)
            self._glide((x + math.cos(ang) * over, y + math.sin(ang) * over))
            self.pause(0.04, 0.12)
        self._glide((x, y))

    def _glide(self, target):
        pts = path(self.pos, target, self.rng)
        per_step = self.rng.uniform(0.006, 0.014) * self.speed
        for px, py in pts:
            self.page.mouse.move(px, py)
            self.sleep(per_step * self.rng.uniform(0.7, 1.3))
        self.pos = pts[-1]

    def _target_point(self, box):
        # Somewhere around the middle of the element, never the exact centre or the edges.
        cx = box["x"] + box["width"] / 2 + self.rng.gauss(0, box["width"] / 8)
        cy = box["y"] + box["height"] / 2 + self.rng.gauss(0, box["height"] / 8)
        cx = min(max(cx, box["x"] + box["width"] * 0.15), box["x"] + box["width"] * 0.85)
        cy = min(max(cy, box["y"] + box["height"] * 0.2), box["y"] + box["height"] * 0.8)
        return cx, cy

    def _box(self, locator):
        locator.wait_for(state="visible")
        box = locator.bounding_box()
        if box is None:
            raise RuntimeError("element has no box")
        if box["y"] < 0 or box["y"] + box["height"] > self.h:
            self.scroll_into_view(locator)
            box = locator.bounding_box() or box
        return box

    def hover(self, locator):
        self.move_to(*self._target_point(self._box(locator)))
        self.pause(0.08, 0.3)

    def click(self, locator):
        self.hover(locator)
        self.page.mouse.down()
        self.pause(0.05, 0.13)
        self.page.mouse.up()
        self.pause(0.1, 0.35)

    # ---------- keyboard ----------
    def key(self, key):
        """Press and release one key with a human hold time; capitals and symbols hold Shift."""
        kb = self.page.keyboard
        shift = len(key) == 1 and key in SHIFTED
        if shift:
            kb.down("Shift")
            self.sleep(self.rng.uniform(0.04, 0.11) * self.speed)
        kb.down(key)
        self.sleep(self._dwell())
        kb.up(key)
        if shift:
            self.sleep(self.rng.uniform(0.02, 0.08) * self.speed)
            kb.up("Shift")

    def _shortcut(self, *keys):
        kb = self.page.keyboard
        for k in keys[:-1]:
            kb.down(k)
            self.sleep(self.rng.uniform(0.05, 0.12) * self.speed)
        self.key(keys[-1])
        for k in reversed(keys[:-1]):
            self.sleep(self.rng.uniform(0.02, 0.07) * self.speed)
            kb.up(k)

    def _field_has_text(self, locator):
        try:
            return bool(locator.input_value(timeout=1000))
        except Exception:
            return True  # unknown: clear to be safe

    def type(self, locator, text, typos=True, clear=False):
        """Click into the field and type like a person, optionally fixing occasional typos."""
        self.click(locator)
        if clear and self._field_has_text(locator):
            mod = "Meta" if "Mac" in (self._platform() or "") else "Control"
            self._shortcut(mod, "a")
            self.pause(0.08, 0.2)
            self.key("Backspace")
        self.pause(0.15, 0.5)
        prev = ""
        for ch in text:
            if typos and self.rng.random() < self.typo_rate:
                wrong = neighbour_key(ch, self.rng)
                if wrong:
                    self.key(wrong)
                    self.sleep(self._key_delay(prev, wrong))
                    if self.rng.random() < 0.35:  # sometimes notice only after the next key
                        self.key(ch)
                        self.pause(0.2, 0.5)
                        self.key("Backspace")
                        self.sleep(self._key_delay("", "x"))
                    else:
                        self.pause(0.15, 0.45)
                    self.key("Backspace")
                    self.sleep(self._key_delay("", "x"))
            self.key(ch)
            self.sleep(self._key_delay(prev, ch))
            prev = ch
        self.pause(0.2, 0.6)

    def press(self, key):
        self.pause(0.1, 0.3)
        self.key(key)

    def _platform(self):
        if not hasattr(self, "_plat"):
            try:
                self._plat = self.page.evaluate("navigator.platform")
            except Exception:
                self._plat = ""
        return self._plat

    # ---------- scrolling / idling ----------
    def scroll(self, dy):
        """Wheel-scroll roughly dy pixels. Every event is one notch of the same size, like a real
        wheel; speed shows up as timing: quick bursts of notches with short gaps, then pauses."""
        notches = max(1, round(abs(dy) / self.WHEEL_NOTCH))
        step = self.WHEEL_NOTCH if dy > 0 else -self.WHEEL_NOTCH
        while notches > 0:
            burst = min(notches, self.rng.randint(1, 4))
            for _ in range(burst):
                self.page.mouse.wheel(0, step)
                self.sleep(self.rng.uniform(0.018, 0.06) * self.speed)
            notches -= burst
            if notches:
                self.sleep(self.rng.uniform(0.08, 0.35) * self.speed)
                if self.rng.random() < 0.12:
                    self.pause(0.3, 0.9)

    def _scroll_y(self):
        try:
            return self.page.evaluate("() => window.scrollY")
        except Exception:
            return None

    def scroll_into_view(self, locator):
        """Wheel-scroll until the element is on screen. Only scrolls when it's actually off-screen,
        and stops when the page stops moving (top/bottom reached, or a fixed header element)."""
        for _ in range(8):
            box = locator.bounding_box()
            if box is None:
                break
            if box["y"] >= 0 and box["y"] + box["height"] <= self.h:
                return
            before = self._scroll_y()
            if box["y"] < 0:
                self.scroll(max(box["y"] - self.h * 0.3, -self.h * 0.8))
            else:
                self.scroll(min(box["y"] + box["height"] - self.h * 0.6, self.h * 0.8))
            self.pause(0.15, 0.4)
            if before is not None and self._scroll_y() == before:
                break
        locator.scroll_into_view_if_needed()

    def wander(self, moves=None):
        """A few aimless mouse movements, like someone reading."""
        for _ in range(moves or self.rng.randint(1, 3)):
            self.move_to(self.rng.uniform(0.1, 0.9) * self.w, self.rng.uniform(0.15, 0.85) * self.h,
                         overshoot=False)
            self.pause(0.2, 1.2)

    def read(self, seconds=None):
        """Look over a page: wander, scroll down a bit, maybe back up, dwell."""
        end = time.monotonic() + (seconds if seconds is not None else self.rng.uniform(2.5, 6.0)) * self.speed
        self.wander(1)
        for _ in range(self.rng.randint(1, 3)):
            self.scroll(self.rng.uniform(150, 600))
            self.pause(0.6, 2.0)
            if self.rng.random() < 0.4:
                self.wander(1)
        if self.rng.random() < 0.3:
            self.scroll(-self.rng.uniform(100, 300))
        remaining = end - time.monotonic()
        if remaining > 0:
            self.sleep(remaining)
