import os
import time

from app import applog, db
from app.config import settings


def _old(p, days):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("x")
    t = time.time() - days * 86400
    os.utime(p, (t, t))
    return p


def test_debug_files_with_personal_data_are_pruned(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "debug_dir", tmp_path / "debug")
    db.init()
    d = settings.debug_dir
    old_page = _old(d / "pages" / "20261101-seat-error.html", 30)
    old_json = _old(d / "search-LIR-JAX.json", 30)
    old_cal = _old(d / "calendar" / "cal.json", 30)
    old_shot = _old(d / "shots" / "2026-11-01" / "x.png", 30)
    kept_shot = _old(d / "shots" / "2026-11-01" / "still-in-log.png", 30)
    new_page = _old(d / "pages" / "20261120-seat-error.html", 1)
    applog.warn("still shown", attachment=kept_shot)
    applog._prune()
    assert not old_page.exists() and not old_json.exists() and not old_cal.exists() and not old_shot.exists()
    assert kept_shot.exists() and new_page.exists()
    assert not (d / "calendar").exists()                                  # emptied folders go too
