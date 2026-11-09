"""Airport names for codes (data: app/static/airports.json, built by scripts/build_airports.py)."""
import json
from functools import lru_cache
from pathlib import Path

PATH = Path(__file__).parent / "static" / "airports.json"


@lru_cache(maxsize=1)
def table():
    try:
        return json.loads(PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def place(code):
    """Southwest-style place for a code, e.g. "Houston (Hobby), TX" for HOU; None if unknown."""
    a = table().get((code or "").upper())
    if not a:
        return None
    return a[4] if len(a) > 4 else f"{a[0]}, {a[1]}"


def describe(codes):
    """[(code, "Dallas, TX" or None)] for a list of codes."""
    return [(c, place(c)) for c in codes]
