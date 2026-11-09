from app import airports, db
from tests.test_web import admin_client, watch_form


def test_place_names():
    assert airports.place("dal") == "Dallas, TX"
    assert airports.place("SJO").endswith("Costa Rica")
    assert airports.place("PBI").endswith(", FL")          # renamed airport keeps its Southwest code
    assert airports.place("ZZZ") is None
    t = airports.table()
    assert len(t) > 3000 and sum(v[3] for v in t.values()) > 100


def test_form_shows_airport_names(client):
    token = admin_client(client)
    client.post("/watches", data=watch_form(token, origin="DAL, QQQ", destination="HOU"), follow_redirects=False)
    wid = db.one("SELECT id FROM watches ORDER BY id DESC")["id"]
    page = client.get(f"/watches/{wid}/edit").text
    assert "<b>DAL</b>: Dallas, TX" in page and "<b>HOU</b>: Houston, TX" in page
    assert 'class="unknown"><b>QQQ</b>: airport not found' in page
    assert client.get("/static/airports.json").status_code == 200


def test_form_fields_offer_airport_suggestions(client):
    admin_client(client)
    page = client.get("/watches/new").text
    assert page.count("data-airport-suggest") == 2 and 'maxlength="60"' in page
