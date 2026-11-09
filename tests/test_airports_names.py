from app import airports, db
from tests.test_web import admin_client, watch_form


def test_place_names():
    assert airports.place("dal") == "Dallas, TX"
    assert airports.place("SJO").endswith("Costa Rica")
    assert airports.place("PBI").endswith(", FL")          # renamed airport keeps its Southwest code
    assert airports.place("HOU") == "Houston (Hobby), TX" and airports.place("MDW") == "Chicago (Midway), IL"
    assert airports.place("MIA") == "Miami, FL"
    assert airports.place("ZZZ") is None
    t = airports.table()
    assert len(t) > 3000 and sum(v[3] for v in t.values()) > 100


def test_form_shows_airport_names(client):
    token = admin_client(client)
    client.post("/watches", data=watch_form(token, origin="DAL, QQQ", destination="HOU"), follow_redirects=False)
    wid = db.one("SELECT id FROM watches ORDER BY id DESC")["id"]
    page = client.get(f"/watches/{wid}/edit").text
    assert "Dallas, TX - DAL" in page and "Houston (Hobby), TX - HOU" in page
    assert "class=\"unknown\">Airport not found - QQQ" in page
    assert client.get("/static/airports.json").status_code == 200


def test_form_fields_offer_airport_suggestions(client):
    admin_client(client)
    page = client.get("/watches/new").text
    assert page.count("data-airport-suggest") == 2 and 'maxlength="60"' in page


def test_form_marks_fields_for_readable_lines(client):
    admin_client(client)
    page = client.get("/watches/new").text
    assert 'class="card form-grid watch-form"' in page
    assert 'name="max_duration_h" data-readable="hours"' in page and 'name="max_points" data-readable="points"' in page
