import datetime as dt
import re

import pytest

from app import db, mailer
from app.config import settings
from app.worker import Worker


def detail(fare, taxes, flights="1234", depart="08:15", stops=0, duration=205):
    return {
        "flightNumbers": flights, "departureTime": depart, "arrivalTime": "12:40",
        "departureDateTime": f"X{flights}{depart}", "totalDuration": duration, "numberOfStops": stops,
        "fareProducts": {"ADULT": {"WGA": {"availabilityStatus": "AVAILABLE", "fare": {
            "totalFare": {"value": fare}, "totalTaxesAndFees": {"value": taxes}}}}},
    }


def payload(*details):
    return {"data": {"searchResults": {"airProducts": [{"details": list(details)}]}}}


class FakeSearcher:
    """Stands in for the browser. cash/points maps fare_type -> parsed flights (or None = failure)."""
    calls = []
    fail = False

    def __init__(self, state=None, profile_key=None):
        self.state = state
        self.profile_key = profile_key

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        if self.state is not None:
            self.state = {**self.state, "refreshed": True}

    def search(self, origin, dest, date, adults, fare_type):
        from app.swa import parse_flights
        FakeSearcher.calls.append((origin, dest, date, fare_type))
        if FakeSearcher.fail:
            return None
        if fare_type == "USD":
            return parse_flights(payload(detail("189.98", "20.00"), detail("420.00", "20", flights="99")))
        return parse_flights(payload(detail("12,000", "5.60"), detail("30,000", "5.60", flights="99")))


@pytest.fixture(autouse=True)
def tmp_data(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "db_path", tmp_path / "app.db")
    monkeypatch.setattr(settings, "debug_dir", tmp_path / "debug")
    monkeypatch.setattr(settings, "admin_username", "admin")
    monkeypatch.setattr(settings, "admin_password", "admin-password-123")
    monkeypatch.setattr(settings, "_secret", "test-secret")
    FakeSearcher.calls = []
    FakeSearcher.fail = False
    db.init()
    yield tmp_path


@pytest.fixture
def sent(monkeypatch):
    out = []
    monkeypatch.setattr(mailer, "send", lambda to, subject, html: out.append((to, subject, html)) or True)
    return out


@pytest.fixture
def worker():
    return Worker(searcher_factory=FakeSearcher, sleep=lambda s: None)


@pytest.fixture
def client(worker):
    from fastapi.testclient import TestClient

    from app.web import create_app

    with TestClient(create_app(worker=worker, start_worker=False)) as c:
        yield c


def csrf(html):
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def login(client, username, password):
    return client.post("/login", data={"username": username, "password": password}, follow_redirects=False)


def future(days):
    return (dt.date.today() + dt.timedelta(days=days)).isoformat()
