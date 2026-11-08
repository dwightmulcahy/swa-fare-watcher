import math
import random

import pytest

from app import human
from app.human import Human, neighbour_key, path


class FakeMouse:
    def __init__(self, log):
        self.log = log

    def move(self, x, y):
        self.log.append(("move", x, y))

    def down(self):
        self.log.append(("down",))

    def up(self):
        self.log.append(("up",))

    def wheel(self, dx, dy):
        self.log.append(("wheel", dy))


class FakeKeyboard:
    """Applies keystrokes to a text buffer like a browser would: a printable key adds its
    character on key-down, Backspace deletes, modifiers add nothing."""

    def __init__(self, log):
        self.log, self.text, self.held = log, "", set()

    def down(self, key):
        self.log.append(("down", key))
        if key == "a" and {"Control", "Meta"} & self.held:
            self.selected = True
        elif key == "Backspace":
            self.text = "" if getattr(self, "selected", False) else self.text[:-1]
            self.selected = False
        elif len(key) == 1 and not ({"Control", "Meta"} & self.held):
            self.text += key
        self.held.add(key)

    def up(self, key):
        self.log.append(("up", key))
        self.held.discard(key)

    def type(self, s):  # must not be used: zero-length key presses are a bot tell
        raise AssertionError("keyboard.type used")

    def press(self, key):
        raise AssertionError("keyboard.press used")


class FakeLocator:
    def __init__(self, box):
        self.box = box

    def wait_for(self, state):
        pass

    def bounding_box(self):
        return self.box

    def scroll_into_view_if_needed(self):
        pass


class FakePage:
    viewport_size = {"width": 1366, "height": 768}

    def __init__(self):
        self.log = []
        self.mouse = FakeMouse(self.log)
        self.keyboard = FakeKeyboard(self.log)


def make(seed=1, **kw):
    page = FakePage()
    slept = []
    return page, Human(page, rng=random.Random(seed), sleep=slept.append, **kw), slept


def test_path_is_curved_and_ends_on_target():
    rng = random.Random(3)
    pts = path((100, 100), (900, 500), rng)
    assert pts[-1] == pytest.approx((900, 500))
    assert 20 <= len(pts) <= 80
    # deviates from the straight line somewhere
    def off_line(p):
        return abs((500 - 100) * p[0] - (900 - 100) * p[1] + 900 * 100 - 500 * 100) / math.hypot(400, 800)
    assert max(off_line(p) for p in pts) > 20
    # eased: first and last steps are shorter than the middle ones
    steps = [math.dist(a, b) for a, b in zip(pts, pts[1:])]
    mid = steps[len(steps) // 2]
    assert steps[0] < mid and steps[-1] < mid


def test_click_lands_inside_element_not_dead_centre():
    hits = []
    for seed in range(40):
        page, h, _ = make(seed)
        box = {"x": 400, "y": 300, "width": 120, "height": 40}
        h.click(FakeLocator(box))
        moves = [e for e in page.log if e[0] == "move"]
        x, y = moves[-1][1:]
        assert 400 + 120 * 0.15 <= x <= 400 + 120 * 0.85 and 300 + 40 * 0.2 <= y <= 300 + 40 * 0.8
        assert [e[0] for e in page.log[-2:]] == ["down", "up"]
        hits.append((round(x), round(y)))
    assert len(set(hits)) > 30  # not the same spot every time


@pytest.mark.parametrize("seed", range(30))
def test_typing_with_typos_ends_with_exact_text(seed):
    page, h, _ = make(seed, typo_rate=0.3)
    text = "Dallas Love Field 2026, Gate 7!"
    h.type(FakeLocator({"x": 10, "y": 10, "width": 200, "height": 30}), text)
    assert page.keyboard.text == text


def test_typos_are_corrected_with_backspace():
    page, h, _ = make(5, typo_rate=0.5)
    h.type(FakeLocator({"x": 10, "y": 10, "width": 200, "height": 30}), "southwest")
    presses = [e for e in page.log if e == ("down", "Backspace")]
    assert presses and page.keyboard.text == "southwest"


def test_no_typos_when_disabled():
    page, h, _ = make(5, typo_rate=0.9)
    h.type(FakeLocator({"x": 10, "y": 10, "width": 200, "height": 30}), "secret-Pass1", typos=False)
    assert ("down", "Backspace") not in page.log


def test_key_timing_is_human_range():
    _, h, _ = make(2)
    dwell = sorted(h._dwell() for _ in range(2000))
    gaps = sorted(h._dwell() + h._key_delay("a", "b") for _ in range(2000))
    assert 0.06 < dwell[1000] < 0.11 and dwell[0] >= 0.035   # keys held ~85 ms, never ~0
    assert 0.12 < gaps[1000] < 0.2                            # key-to-key ~160 ms (~70 wpm)
    assert len({round(d, 3) for d in gaps}) > 150             # not a fixed cadence


def test_keys_are_held_and_capitals_use_shift():
    page, h, slept = make(9, typo_rate=0)
    h.type(FakeLocator({"x": 10, "y": 10, "width": 200, "height": 30}), "Hi TX!")
    keys = [e for e in page.log if e[0] in ("down", "up") and len(e) == 2]  # keyboard only
    # every key goes down then up, never a zero-length press
    assert [e for e in keys if e[1] == "H"] == [("down", "H"), ("up", "H")]
    i = keys.index(("down", "H"))
    assert keys[i - 1] == ("down", "Shift") and keys[i + 2] == ("up", "Shift")
    assert ("down", "Shift") not in keys[keys.index(("down", "i")) - 1: keys.index(("down", "i"))]
    assert keys.count(("down", "Shift")) == 4  # H, T, X, !
    assert page.keyboard.text == "Hi TX!"


def test_empty_field_is_not_select_all_cleared():
    page, h, _ = make(1)
    loc = FakeLocator({"x": 10, "y": 10, "width": 200, "height": 30})
    loc.input_value = lambda timeout=None: ""
    h.type(loc, "abc", typos=False, clear=True)
    assert ("down", "Control") not in page.log and ("down", "Meta") not in page.log
    loc.input_value = lambda timeout=None: "old"
    page.keyboard.text = "old"
    h.type(loc, "abc", typos=False, clear=True)
    assert ("down", "Control") in page.log and page.keyboard.text == "abc"


def test_pointer_enters_from_the_top():
    for seed in range(20):
        _, h, _ = make(seed)
        assert h.pos[1] <= 12


def test_scroll_uses_one_fixed_wheel_notch():
    page, h, _ = make(4)
    h.scroll(700)
    h.scroll(-250)
    wheels = [e[1] for e in page.log if e[0] == "wheel"]
    assert set(wheels) == {100, -100}          # a physical wheel: always the same step
    assert wheels.count(100) == 7 and wheels.count(-100) == 2


def test_tremor_is_independent_per_axis():
    import itertools

    class R(random.Random):
        def __init__(self, vals):
            super().__init__(1)
            self.vals = vals

        def gauss(self, mu, sigma):
            return next(self.vals)

    still = path((0, 0), (300, 100), R(itertools.repeat(0.0)), steps=12)
    shaky = path((0, 0), (300, 100), R(itertools.cycle([0.5, -0.5])), steps=12)
    offsets = {(round(a[0] - b[0], 6), round(a[1] - b[1], 6)) for a, b in zip(shaky[:-1], still[:-1])}
    assert offsets == {(0.5, -0.5)}  # x and y take separate draws (the old code moved both by the same amount)
    assert shaky[-1] == still[-1]    # and the path still ends exactly on target


def test_neighbour_keys():
    rng = random.Random(1)
    assert neighbour_key("a", rng) in "qwszx"
    assert neighbour_key("A", rng).isupper()
    assert neighbour_key("!", rng) is None
    assert "g" not in human._NEIGHBOURS["g"] and {"f", "h", "t", "b"} <= set(human._NEIGHBOURS["g"])
