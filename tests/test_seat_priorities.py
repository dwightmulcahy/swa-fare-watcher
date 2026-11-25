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
    "bulkhead_window": seat("1F", "extra", "window", "bulkhead seat"),
    "exit_aisle": seat("14C", "standard", "aisle", "exit row"),
    "extra_aisle": seat("2C", "extra", "aisle"),
    "bulkhead_aisle": seat("1D", "extra", "aisle", "bulkhead seat"),
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
    assert seats.priority(bulk, D)[1] == "bulkhead_aisle"                 # bulkhead is its own seat type
    assert not seats.matches(bulk, "extra_aisle") and not seats.attributes(bulk)["extra"]   # not assumed extra
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


def test_bulkhead_can_be_ranked_or_switched_off():
    no_bulk = seats.normalize({"priorities": {**seats.DEFAULT_PRIORITIES, "bulkhead_window": None,
                                              "bulkhead_aisle": None}})
    pool = [EXAMPLES["bulkhead_window"], EXAMPLES["std_window"]]
    assert seats.choose(pool, D) is EXAMPLES["bulkhead_window"]          # default: above standard
    assert seats.choose(pool, no_bulk) is EXAMPLES["std_window"]         # Off: never the bulkhead
    first = seats.normalize({"priorities": {**seats.DEFAULT_PRIORITIES, "bulkhead_window": 1, "exit_window_open": 2}})
    assert seats.choose([EXAMPLES["exit_window_open"], EXAMPLES["bulkhead_window"]], first) is EXAMPLES["bulkhead_window"]


def test_watches_saved_before_bulkhead_keep_treating_it_as_extra_legroom():
    old = {"version": 2, "priorities": {"exit_window_open": 1, "exit_window": 2, "extra_window": 3, "exit_aisle": 4,
                                        "extra_aisle": None, "std_window": 6, "std_aisle": 7, "middle": None},
           "tiebreak": ["position", "legroom"]}
    p = seats.normalize(old)
    assert p["priorities"]["bulkhead_window"] == 3 and p["priorities"]["bulkhead_aisle"] is None
    assert p["tiebreak"] == ["position", "legroom", "recline", "neighbor"]  # new tie-breakers on for old lists
    again = seats.normalize(json.loads(seats.to_json({**p, "tiebreak": ["position"]})))
    assert again["tiebreak"] == ["position"]                              # ... but removing it sticks


def test_planes_and_recline():
    assert seats.plane_name("Boeing 737 MAX8 (subject to change)*") == "Boeing 737 MAX 8"
    assert seats.plane_name("Boeing 737-700") == "Boeing 737-700" and seats.plane_name("Airbus") is None
    maps = seats.example_maps()
    max8 = {s["seat"]: s for s in maps["Boeing 737 MAX 8"]}
    assert len(max8) == 175 and "15A" not in max8 and "1A" not in max8
    assert {k for k, s in max8.items() if seats.OPEN_FRONT in s["features"]} == {"16A", "16F"}
    assert "window without window view" in max8["10A"]["features"] and max8["6A"]["type"] == "extra"
    assert max8["6D"]["type"] == "preferred" and max8["17A"]["type"] == "standard"
    assert seats.LIMITED_RECLINE in max8["30C"]["features"] and seats.LIMITED_RECLINE in max8["13A"]["features"]
    assert seats.LIMITED_RECLINE not in max8["12A"]["features"]
    bulk = {k for k, s in max8.items() if "bulkhead seat" in s["features"]}
    assert bulk == {"1D", "1E", "1F", "2A", "2B", "2C"}                  # as on Southwest's map
    no_bulk = seats.normalize({"priorities": {**seats.DEFAULT_PRIORITIES, "bulkhead_window": None,
                                              "bulkhead_aisle": None}})
    assert seats.priority({**max8["2A"], "features": set(max8["2A"]["features"])}, no_bulk) == (None, None)
    p = seats.normalize({"priorities": {"std_window": 1}, "tiebreak": ["recline"]})
    s12, s13 = (seat("12A", "standard", "window"), dict(seat("13A", "standard", "window"),
                                                        features={"window", seats.LIMITED_RECLINE}))
    assert seats.choose([s13, s12], p) is s12                             # tie: the one that reclines
    plain = [seat("9A", "standard", "window"), seat("20A", "standard", "window")]
    seats.add_plane_facts(plain, None)
    assert seats.LIMITED_RECLINE in plain[1]["features"]                  # last row on the map, any plane


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
    data = {"csrf": t, "kind": "seats", "booking_conf": "ABC123", "booking_first": "Pat", "booking_last": "Lee", "booking_email": "pat@x.com",
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
    assert 'name="seat_fallback" value="1" aria-describedby="tip-fallback" checked' in edit
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


def test_form_explains_fallback_and_explanation(client):
    from tests.test_web import admin_client
    admin_client(client)
    page = client.get("/watches/new?kind=seats").text
    assert 'aria-describedby="tip-fallback"' in page and "never taken" in page and "fallback" in page
    assert 'id="tip-explain"' in page and "Selected: 3A" in page



def test_737_700_skips_rows_10_to_13():
    m = {x["seat"]: x for x in seats.example_maps()["Boeing 737-700"]}
    rows = sorted({x["row"] for x in m.values()})
    assert rows == list(range(1, 10)) + list(range(14, 28)) and len(m) == 138
    assert {k for k, x in m.items() if "bulkhead seat" in x["features"]} == {f"1{c}" for c in "ABCDEF"}
    assert {k for k, x in m.items() if "exit row" in x["features"]} == {f"15{c}" for c in "ABCDEF"}


def test_seat_presets_save_list_apply_and_delete(client):
    import json

    from app import db, seats
    from tests.conftest import csrf
    from tests.test_web import admin_client
    t = admin_client(client)
    page = client.get("/watches/new?kind=seats").text
    data = json.loads(page.split("data-presets='")[1].split("'")[0].replace("&#34;", '"'))
    assert [p["name"] for p in data] == list(seats.BUILTIN_PRESETS)          # ready-made ones first
    assert data[1]["prefs"]["position"] == "aisle" and data[0]["prefs"]["priorities"]["middle"] is None
    form = {"csrf": t, "seat_position": "aisle", "prio_exit_aisle": "1", "prio_std_aisle": "2",
            "tiebreak_order": "legroom,position", "tb_legroom": "1", "seat_fallback": "1"}
    r = client.post("/seat-presets", data={**form, "preset_name": "  Lisa   short hops "})
    assert r.json()["ok"] and r.json()["presets"][-1]["name"] == "Lisa short hops"
    saved = r.json()["presets"][-1]
    assert saved["prefs"]["priorities"]["exit_aisle"] == 1 and saved["prefs"]["priorities"]["middle"] is None
    assert saved["prefs"]["tiebreak"] == ["legroom"] and saved["prefs"]["fallback"] is True
    # same name replaces; built-in names and blank names are refused; other users can't delete it
    client.post("/seat-presets", data={**form, "prio_middle": "3", "preset_name": "Lisa short hops"})
    assert db.one("SELECT COUNT(*) n FROM seat_presets")["n"] == 1
    assert client.post("/seat-presets", data={**form, "preset_name": "window FIRST"}).status_code == 400
    assert client.post("/seat-presets", data={**form, "preset_name": " "}).status_code == 400
    assert client.post("/seat-presets", data={"preset_name": "x"}).status_code == 403          # no csrf
    assert "Lisa short hops" in client.get("/watches/new?kind=seats").text
    r = client.post(f"/seat-presets/{saved['id']}/delete", data={"csrf": csrf(page)})
    assert r.json()["ok"] and not db.one("SELECT 1 FROM seat_presets")


def test_empty_seat_next_to_it_breaks_ties():
    def s(code, free=True, mine=False):
        return {"seat": code, "row": int(code[:-1]), "col": code[-1], "type": "standard", "price": 0,
                "features": {"window" if code[-1] in "AF" else "aisle" if code[-1] in "CD" else "middle"},
                "available": free, "selected": mine}
    m = seats.annotate([s("8D"), s("8E", free=False), s("8F", free=False, mine=True),
                        s("9D", free=False), s("9E"), s("9F"), s("10B"), s("10A", free=False), s("10C", free=False)])
    has = {x["seat"] for x in m if seats.EMPTY_NEIGHBOR in x["features"]}
    assert "9F" in has and "8F" not in has and "8D" not in has      # 9E free beside 9F; 8E taken beside 8F
    assert "9D" in has                                             # its neighbour 9E is free (taken itself)
    assert "8E" in has                                             # 8D free and 8F is your own seat
    assert "10B" not in has                                        # middle: both 10A and 10C taken
    prefs = seats.normalize({"priorities": {"std_window": 1}, "tiebreak": ["neighbor"], "tiebreak_v3": True})
    cur, other = next(x for x in m if x["seat"] == "8F"), next(x for x in m if x["seat"] == "9F")
    assert seats.choose(m, prefs)["seat"] == "9F" and seats.better(other, cur, prefs)
    assert "The seat next to it is free" in seats.explain(other, prefs)
    off = {**prefs, "tiebreak": []}
    assert not seats.better(other, cur, off)                        # tie-breaker off: just another seat
    assert seats.normalize({})["tiebreak"][:3] == ["position", "legroom", "neighbor"]


def test_travelers_save_list_and_delete(client):
    import json

    from app import db
    from tests.conftest import csrf
    from tests.test_web import admin_client
    t = admin_client(client)
    form = {"csrf": t, "booking_first": "Lisa", "booking_last": "Fern", "booking_email": "lisa@x.com",
            "seat_status": "a_list_preferred", "seat_position": "aisle", "prio_exit_aisle": "1",
            "tiebreak_order": "neighbor,legroom", "tb_neighbor": "1"}
    r = client.post("/travelers", data=form).json()
    assert r["ok"] and r["name"] == "Lisa F."                                 # default name: first + initial
    x = r["travelers"][0]
    assert (x["first"], x["last"], x["email"], x["status"]) == ("Lisa", "Fern", "lisa@x.com", "a_list_preferred")
    assert x["prefs"]["position"] == "aisle" and x["prefs"]["tiebreak"] == ["neighbor"]
    assert x["prefs"]["priorities"]["exit_aisle"] == 1
    r = client.post("/travelers", data={**form, "traveler_name": "Lisa F.", "seat_status": "bogus"}).json()
    assert len(r["travelers"]) == 1 and r["travelers"][0]["status"] == "none"   # same name: replaced
    assert client.post("/travelers", data={**form, "booking_first": ""}).status_code == 400
    assert client.post("/travelers", data={**form, "booking_email": "nope"}).status_code == 400
    page = client.get("/watches/new?kind=seats").text
    data = json.loads(page.split("data-travelers='")[1].split("'")[0].replace("&#34;", '"'))
    assert data[0]["name"] == "Lisa F."
    assert client.post(f"/travelers/{x['id']}/delete", data={"csrf": csrf(page)}).json()["travelers"] == []
    assert not db.one("SELECT 1 FROM travelers")


def test_a_whole_empty_side_of_the_row_beats_one_empty_seat_beside_you():
    def s(code, free=True):
        return {"seat": code, "row": int(code[:-1]), "col": code[-1], "type": "standard", "price": 0,
                "features": {"window" if code[-1] in "AF" else "aisle" if code[-1] in "CD" else "middle"},
                "available": free, "selected": False}
    m = seats.annotate([s("7A"), s("7B"), s("7C"),                 # whole side free
                        s("8A"), s("8B"), s("8C", free=False),     # just the middle free
                        s("9A"), s("9B", free=False), s("9C")])    # nobody-free beside 9A
    by = {x["seat"]: x for x in m}
    assert seats.EMPTY_SIDE in by["7A"]["features"] and seats.EMPTY_SIDE not in by["8A"]["features"]
    assert seats.EMPTY_NEIGHBOR in by["8A"]["features"] and seats.EMPTY_NEIGHBOR not in by["9A"]["features"]
    prefs = seats.normalize({"priorities": {"std_window": 1}, "tiebreak": ["neighbor"], "tiebreak_v3": True})
    assert [x["seat"] for x in seats.ranked(m, prefs, "none", set(), True)][:3] == ["7A", "8A", "9A"]
    assert seats.better(by["7A"], by["8A"], prefs) and "rest of its side of the row is free" in seats.explain(by["7A"], prefs)


def test_edge_names_the_tie_breaker_that_decided():
    def s(code, feats=(), typ="extra"):
        return {"seat": code, "row": int(code[:-1]), "col": code[-1], "type": typ, "price": 0,
                "features": {"window", *feats}, "available": True, "selected": False}
    prefs = seats.normalize({"priorities": {"extra_window": 1, "std_window": 2},
                             "tiebreak": ["legroom", "neighbor", "forward"], "tiebreak_v3": True})
    assert seats.edge(s("4F"), s("15A"), prefs) == "further forward in the cabin"
    assert seats.edge(s("14F", [seats.EMPTY_NEIGHBOR]), s("4A"), prefs) == "the seat next to it is free"
    assert seats.edge(s("14F", [seats.EMPTY_NEIGHBOR, seats.EMPTY_SIDE]), s("4A", [seats.EMPTY_NEIGHBOR]), prefs) \
        == "the rest of its side of the row is free"
    assert seats.edge(s("4A"), s("4F"), prefs) == "a lower seat number"
    assert seats.edge(s("15A"), s("4F"), prefs) is None                         # it's the other way round
    assert seats.edge(s("4F"), s("4A", typ="standard"), prefs) is None          # different priority: shown already
    from app.worker import seat_results
    r = {"passenger": "Lisa F.", "current": {"seat": "4F", "label": "Priority 1"}, "picked": None,
         "options": [{"seat": "5A", "label": "Priority 1"}], "edge": "further forward in the cabin"}
    assert seat_results([r], []).endswith("(same priority; this one has further forward in the cabin)")
