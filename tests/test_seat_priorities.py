"""Seat Selection Priorities: classification, strict priority order, ties, Off, fallback,
explanations, persistence and migration."""
import itertools
import json

import pytest

from app import seats

D = seats.normalize({})


def seat(code, kind="standard", *features, available=True):
    row, col = int(code[:-1]), code[-1]
    return {"seat": code, "row": row, "col": col, "type": kind, "features": set(features), "price": 0,
            "available": available, "selected": False}


# one seat per category, each made as attractive on the secondary criteria as the category allows
EXAMPLES = {
    "exit_window_open": seat("16A", "standard", "window", "exit row", seats.OPEN_FRONT),
    "exit_window": seat("14F", "standard", "window", "exit row"),
    "extra_window": seat("3A", "extra", "window"),
    "exit_aisle": seat("14C", "standard", "aisle", "exit row"),
    "extra_aisle": seat("2C", "extra", "aisle"),
    "std_window": seat("4A", "preferred", "window"),
    "std_aisle": seat("4C", "preferred", "aisle"),
    "middle": seat("2B", "extra", "middle"),
}


@pytest.mark.parametrize("cat", list(seats.CATEGORIES))
def test_every_default_priority(cat):
    assert seats.priority(EXAMPLES[cat], D) == (seats.DEFAULT_PRIORITIES[cat], cat)


@pytest.mark.parametrize("hi,lo", list(itertools.combinations(seats.CATEGORIES, 2)))
def test_higher_priority_always_wins_every_pair(hi, lo):
    """The lower-priority seat is moved to row 1 with every secondary advantage it can have."""
    better_side = dict(EXAMPLES[lo], row=1, seat="1" + EXAMPLES[lo]["col"])
    better_side["features"] = set(EXAMPLES[lo]["features"])
    a, b = EXAMPLES[hi], better_side
    assert seats.choose([b, a], D) is a and seats.choose([a, b], D) is a
    assert seats.better(a, b, D) and not seats.better(b, a, D)


def test_spec_examples():
    assert seats.choose([EXAMPLES["exit_aisle"], EXAMPLES["extra_window"]], D) is EXAMPLES["extra_window"]  # 3 v 4
    assert seats.choose([EXAMPLES["extra_aisle"], EXAMPLES["exit_aisle"]], D) is EXAMPLES["exit_aisle"]     # 4 v 5
    front_std = seat("1F", "preferred", "window")
    assert seats.choose([front_std, seat("20A", "standard", "window", "exit row")], D)["seat"] == "20A"


def test_multiple_attributes_take_the_most_specific_category():
    both = seat("14A", "extra", "window", "exit row")                 # exit row *and* extra legroom
    assert seats.priority(both, D) == (2, "exit_window")                # evaluated against both: 2 beats 3
    assert seats.matches(both, "extra_window") and seats.matches(both, "exit_window")
    assert not seats.matches(both, "exit_window_open")                  # an ordinary exit-row window
    opened = seat("16A", "extra", "window", "exit row", seats.OPEN_FRONT)
    assert seats.priority(opened, D) == (1, "exit_window_open")


def test_never_assumed():
    exit_std = seat("14A", "standard", "window", "exit row")
    assert not seats.matches(exit_std, "extra_window")                   # exit row is not extra legroom
    bulk = seat("1D", "standard", "aisle", "bulkhead seat")
    assert seats.priority(bulk, D)[1] == "std_aisle"                      # bulkhead is not extra legroom
    unknown = seat("5A", "standard")                                      # map doesn't say the position
    assert seats.priority(unknown, D) == (None, None) and seats.attributes(unknown)["position"] is None
    assert seats.attributes(seat("5C", "standard", "aisle"))["view"] is None     # view only known for windows


def test_missing_higher_priorities_fall_through():
    pool = [EXAMPLES["std_aisle"], EXAMPLES["std_window"], EXAMPLES["middle"]]
    assert seats.choose(pool, D) is EXAMPLES["std_window"]
    pool = [EXAMPLES["middle"]]
    assert seats.choose(pool, D) is EXAMPLES["middle"]


def test_equal_priorities_use_tiebreakers_never_across_priorities():
    eq = seats.normalize({"priorities": {"extra_window": 1, "exit_aisle": 1, "std_window": 2},
                          "position": "aisle", "tiebreak": ["position", "legroom"]})
    w, a = EXAMPLES["extra_window"], EXAMPLES["exit_aisle"]
    assert seats.choose([w, a], eq) is a                                  # tie: aisle preferred
    eq2 = {**eq, "tiebreak": ["legroom", "position"]}
    assert seats.choose([a, w], eq2) is w                                 # tie: legroom first
    std = dict(EXAMPLES["std_window"], row=1)                             # priority 2 with every advantage
    assert seats.choose([std, a], eq2) is a


def test_deterministic_final_tiebreak():
    x, y = seat("9A", "standard", "window"), seat("9F", "standard", "window")
    assert seats.choose([y, x], D) is x and seats.choose([x, y], D) is x
    assert not seats.better(y, x, D) and not seats.better(x, y, D)        # another seat number isn't better


def test_off_is_excluded_unless_fallback():
    no_mid = seats.normalize({"priorities": {**seats.DEFAULT_PRIORITIES, "middle": None}})
    pool = [EXAMPLES["middle"]]
    assert seats.choose(pool, no_mid) is None                              # never a middle the user excluded
    fb = {**no_mid, "fallback": True}
    pick = seats.choose(pool, fb)
    assert pick is EXAMPLES["middle"] and seats.label(pick, fb) == "Not in your priorities"
    text = seats.explain(pick, fb)
    assert "fallback" in text and "Middle seat is Off" in text
    assert not seats.better(EXAMPLES["middle"], EXAMPLES["std_aisle"], fb)   # a fallback never beats a match


def test_explanation():
    pool = [EXAMPLES["extra_window"], EXAMPLES["std_window"]]
    pick = seats.choose(pool, D)
    text = seats.explain(pick, D)
    assert text.startswith("Selected: 3A. Matched Priority 3: Extra-legroom window.")
    assert "Higher-ranked Exit-row window, no seat in front, Exit-row window seats were unavailable." in text


def test_status_still_decides_what_is_free():
    pool = [EXAMPLES["extra_window"], EXAMPLES["std_window"]]
    assert seats.choose(pool, D, "premier") is EXAMPLES["std_window"]      # Extra Legroom isn't free for it
    assert seats.choose(pool, D, "a_list", window=False) is EXAMPLES["std_window"]
    assert seats.choose([dict(EXAMPLES["extra_window"], price=39)], D, "a_list_preferred") is None


def test_priorities_persist_and_autosave(client):
    from app import db
    from tests.conftest import future
    from tests.test_web import admin_client
    t = admin_client(client)
    data = {"csrf": t, "kind": "seats", "booking_conf": "ABC123", "booking_first": "Pat", "booking_last": "Lee",
            "origin": "LIR", "destination": "HOU", "trip_type": "oneway", "out_date": future(10),
            "out_time": "13:15", "seat_status": "a_list_preferred", "seat_position": "window", "active": "1"}
    loc = client.post("/watches", data=data, follow_redirects=False).headers["location"].split("?")[0]
    wid = int(loc.rstrip("/").split("/")[-1])
    assert json.loads(db.one("SELECT seat_prefs FROM watches WHERE id=?", (wid,))["seat_prefs"])["priorities"] \
        == seats.DEFAULT_PRIORITIES                                         # no list sent: the defaults
    form = {"csrf": t, "seat_position": "aisle", "tiebreak_order": "forward,view", "tb_forward": "1",
            **{f"prio_{k}": "" for k in seats.CATEGORIES}, "prio_std_aisle": "1", "prio_middle": "2", "seat_fallback": "1"}
    r = client.post(f"/watches/{wid}/seat-prefs", data=form)
    assert r.status_code == 200 and r.json()["ok"]
    p = seats.prefs_of(db.one("SELECT * FROM watches WHERE id=?", (wid,)))
    assert p["priorities"]["std_aisle"] == 1 and p["priorities"]["middle"] == 2 and p["priorities"]["std_window"] is None
    assert p["tiebreak"] == ["forward"] and p["fallback"] and not p["explain"] and p["position"] == "aisle"
    edit = client.get(f"/watches/{wid}/edit").text                       # restored on the next visit
    assert edit.index('name="prio_std_aisle"') < edit.index('name="prio_middle"') < edit.index('name="prio_exit_window"')
    assert 'name="seat_fallback" value="1" checked' in edit
    assert client.post(f"/watches/{wid}/seat-prefs", data={**form, "csrf": "nope"}).status_code == 403


def test_migrated_watch_is_flagged_on_the_form(client):
    from app import db
    from tests.test_seats import _seat_watch
    from tests.conftest import future
    wid, _ = _seat_watch(client, future(10))
    with db.tx() as con:
        con.execute("UPDATE watches SET seat_prefs=? WHERE id=?",
                    (json.dumps({"position": "window", "order": ["no_middle", "extra_legroom"]}), wid))
    edit = client.get(f"/watches/{wid}/edit").text
    assert "Converted from your earlier seat preferences" in edit
    assert 'name="prio_middle"' in edit and edit.count("selected>Off") >= 1
