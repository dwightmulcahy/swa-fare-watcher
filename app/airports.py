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
    """"Dallas, TX" for DAL, or None if the code isn't known."""
    a = table().get((code or "").upper())
    return f"{a[0]}, {a[1]}" if a else None


def describe(codes):
    """[(code, "Dallas, TX" or None)] for a list of codes."""
    return [(c, place(c)) for c in codes]
