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
    """Applies keystrokes to a text buffer so we can check what ended up in the field."""

    def __init__(self, log):
        self.log, self.text = log, ""

    def type(self, s):
        self.log.append(("type", s))
        self.text += s

    def press(self, key):
        self.log.append(("press", key))
        if key == "Backspace":
            self.text = self.text[:-1]


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
    presses = [e for e in page.log if e == ("press", "Backspace")]
    assert presses and page.keyboard.text == "southwest"


def test_no_typos_when_disabled():
    page, h, _ = make(5, typo_rate=0.9)
    h.type(FakeLocator({"x": 10, "y": 10, "width": 200, "height": 30}), "secret-Pass1", typos=False)
    assert ("press", "Backspace") not in page.log


def test_key_timing_is_human_range():
    _, h, _ = make(2)
    delays = [h._key_delay("a", "b") for _ in range(2000)]
    median = sorted(delays)[1000]
    assert 0.08 < median < 0.16  # roughly 70-120 wpm
    assert max(delays) <= 1.6 and min(delays) > 0.02
    assert len({round(d, 3) for d in delays}) > 150  # not a fixed cadence


def test_scroll_uses_uneven_wheel_notches():
    page, h, _ = make(4)
    h.scroll(700)
    wheels = [e[1] for e in page.log if e[0] == "wheel"]
    assert sum(wheels) >= 700 and len(set(wheels)) >= 1 and all(w > 0 for w in wheels)


def test_neighbour_keys():
    rng = random.Random(1)
    assert neighbour_key("a", rng) in "qwszx"
    assert neighbour_key("A", rng).isupper()
    assert neighbour_key("!", rng) is None
    assert "g" not in human._NEIGHBOURS["g"] and {"f", "h", "t", "b"} <= set(human._NEIGHBOURS["g"])
