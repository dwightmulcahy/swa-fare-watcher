import datetime as dt
import io
import json

import pytest

from app import tzcheck
from app.config import settings

WINTER = dt.datetime(2026, 1, 15, 12, tzinfo=dt.timezone.utc)


@pytest.fixture
def tz(monkeypatch):
    def set_tz(name, swa=None):
        monkeypatch.setenv("TZ", name) if name else monkeypatch.delenv("TZ", raising=False)
        monkeypatch.setattr(settings, "browser_timezone", swa)
    monkeypatch.delenv("SWA_SKIP_TZ_CHECK", raising=False)
    return set_tz


def found(zone, ip="203.0.113.7", place="Dallas, Texas, US"):
    return lambda: (zone, ip, place, "ipinfo.io")


def test_matching_zone_ok(tz):
    tz("America/Chicago")
    r = tzcheck.check(found("America/Chicago"), WINTER)
    assert r.level == "ok" and "Dallas" in r.message


def test_different_name_same_offsets_ok(tz):
    tz("America/Winnipeg")  # same as Chicago in winter and summer
    r = tzcheck.check(found("America/Chicago"), WINTER)
    assert r.level == "ok" and "same offsets as America/Chicago" in r.message


def test_docker_default_utc_is_an_error(tz):
    tz("UTC")
    r = tzcheck.check(found("America/Chicago"), WINTER)
    assert r.level == "error" and not r.ok
    assert "UTC+00:00" in r.message and "UTC-06:00" in r.message and "TZ=America/Chicago" in r.message


def test_daylight_saving_difference_is_an_error(tz):
    # Costa Rica and Chicago are both UTC-6 in winter, but Chicago moves to UTC-5 in summer
    tz("America/Costa_Rica")
    r = tzcheck.check(found("America/Chicago"), WINTER)
    assert r.level == "error"


def test_swa_timezone_takes_precedence_and_is_named_in_the_fix(tz):
    tz("UTC", swa="Asia/Tokyo")
    r = tzcheck.check(found("America/Chicago"), WINTER)
    assert r.browser_tz == "Asia/Tokyo" and "SWA_TIMEZONE=America/Chicago" in r.message


def test_lookup_unavailable_warns_but_does_not_block(tz):
    tz("America/Chicago")
    r = tzcheck.check(lambda: None, WINTER)
    assert r.level == "warn" and r.ok


def test_unknown_zone_name_is_an_error(tz):
    tz("Mars/Olympus_Mons")
    assert tzcheck.check(found("America/Chicago"), WINTER).level == "error"


def test_enforce_and_skip(tz, monkeypatch):
    tz("UTC")
    monkeypatch.setattr(tzcheck, "check", lambda: tzcheck.Result("error", "mismatch"))
    assert tzcheck.enforce(log=lambda m: None) is False
    monkeypatch.setenv("SWA_SKIP_TZ_CHECK", "true")
    assert tzcheck.enforce(log=lambda m: None) is True


def test_lookup_falls_through_services():
    calls = []

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    def opener(req, timeout):
        calls.append(req.full_url)
        if "ipinfo" in req.full_url:
            raise OSError("down")
        return Resp(json.dumps({"timezone": "America/Denver", "ip": "198.51.100.1", "city": "Denver",
                                "region": "Colorado", "country_name": "United States"}).encode())

    assert tzcheck.lookup_ip_timezone(opener=opener) == ("America/Denver", "198.51.100.1",
                                                          "Denver, Colorado, United States", "ipapi.co")
    assert len(calls) == 2


def test_utc_in_docker_explains_env_not_reaching_container(tz, monkeypatch):
    tz("UTC")
    monkeypatch.setattr(tzcheck, "in_docker", lambda: True)
    r = tzcheck.check(found("America/Costa_Rica"), WINTER)
    assert r.level == "error" and "env_file:" in r.message and "required: false" in r.message
    assert "NAS's own time zone" in r.message
    monkeypatch.setattr(tzcheck, "in_docker", lambda: False)
    assert "env_file" not in tzcheck.check(found("America/Costa_Rica"), WINTER).message
