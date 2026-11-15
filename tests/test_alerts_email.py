from app import alerts, applog
from app.config import settings
from tests.test_worker import make_user


def _reset():
    alerts._recent.clear()
    alerts._sent.clear()


def test_warning_email_with_screenshot_and_json(sent_with_files, monkeypatch):
    _reset()
    monkeypatch.setattr(settings, "email_warnings", "warnings")
    make_user(username="adm", email="adm@example.com", is_admin=1)
    shot = settings.data_dir / "debug" / "shots" / "x.png"
    raw = settings.data_dir / "debug" / "LIR-HOU-2026-12-12-POINTS.json"
    shot.parent.mkdir(parents=True, exist_ok=True)
    shot.write_bytes(b"png")
    raw.write_text("{}")
    monkeypatch.setattr(alerts, "_start", lambda: None)
    applog.warn("LIR-HOU-2026-12-12-POINTS: unknown shape; raw JSON saved to debug/LIR-HOU-2026-12-12-POINTS.json",
                source="search", attachment=shot)
    alerts.send(*alerts._q.get_nowait())
    to, subject, body, files = sent_with_files[0]
    assert to == "adm@example.com" and subject.startswith("SWA fare watcher warn: LIR-HOU")
    assert sorted(p.name for p in files) == ["LIR-HOU-2026-12-12-POINTS.json", "x.png"]
    applog.warn("LIR-HOU-2026-12-13-POINTS: unknown shape; raw JSON saved to debug/other.json", source="search")
    assert alerts._q.empty()                       # same message shape within the hour: not again


def test_levels_and_skipped_sources(monkeypatch):
    _reset()
    monkeypatch.setattr(settings, "email_warnings", "errors")
    assert not alerts.wanted("warn", "search") and alerts.wanted("error", "search")
    monkeypatch.setattr(settings, "email_warnings", "warnings")
    assert alerts.wanted("warn", "search") and not alerts.wanted("info", "search")
    assert not alerts.wanted("error", "mail")      # never email about email failing
    monkeypatch.setattr(settings, "email_warnings", "off")
    assert not alerts.wanted("error", "search")


def test_hourly_cap(monkeypatch):
    _reset()
    monkeypatch.setattr(settings, "email_warnings", "warnings")
    monkeypatch.setattr(alerts, "_start", lambda: None)
    got = [alerts.notify("warn", "search", f"problem kind {chr(65 + i)}", None) for i in range(20)]
    assert sum(got) == alerts.MAX_PER_HOUR
    while not alerts._q.empty():
        alerts._q.get_nowait()
