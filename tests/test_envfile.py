import os
import re
from pathlib import Path

from app import envfile, envspec

ROOT = Path(__file__).resolve().parent.parent


def test_every_setting_the_app_reads_is_in_the_spec_with_its_default():
    src = (ROOT / "app" / "config.py").read_text()
    found = re.findall(r'_(?:env|bool)\("([A-Z_]+)"(?:,\s*("[^"]*"|True|False))?', src)
    assert len(found) > 40
    for name, default in found:
        assert name in envspec.ALL, f"{name} missing from app/envspec.py"
        if name == "SWA_DATA_DIR":
            continue          # code default is ./data; the image sets /data
        want = {"True": "true", "False": "false"}.get(default, default.strip('"'))
        assert envspec.ALL[name].default == want, name
    for name in ("SWA_USERNAME", "SWA_PASSWORD", "SWA_SKIP_TZ_CHECK", "TZ", "IMAGE", "HOST_PORT", "ENV_FILE"):
        assert name in envspec.ALL


def test_example_env_lists_every_setting():
    text = (ROOT / ".env.example").read_text()
    for name in envspec.ALL:
        assert re.search(rf"^(# )?{name}=", text, re.M), name


def test_quote_round_trips_awkward_values():
    vals = {"A": "plain", "B": "has space", "C": "pa$$word;#x", "D": "it's \"quoted\" $HOME", "E": ""}
    back = envfile.parse(envfile.render(vals))
    assert {k: back[k] for k in vals} == vals
    assert envfile.parse("X=1 # note\nexport Y='a b'\n# Z=3\nW=\"q\\\"r\"") == {"X": "1", "Y": "a b", "W": 'q"r'}


def test_render_comments_every_setting_and_keeps_unknown_ones():
    text = envfile.render({"BASE_URL": "https://x.example", "MY_OWN": "1"})
    assert "BASE_URL=https://x.example" in text and "# SESSION_DAYS=14" in text
    assert "# Public URL of the app" in text and "MY_OWN=1" in text.split("Other settings")[1]


def test_write_keeps_the_file_in_place(tmp_path):
    p = tmp_path / ".env"
    p.write_text("BASE_URL=http://a\n" + "X" * 5000)
    ino = os.stat(p).st_ino
    envfile.write({"BASE_URL": "http://b"}, p)
    assert os.stat(p).st_ino == ino and envfile.read(p)["BASE_URL"] == "http://b"
    assert "XXXX" not in p.read_text()


def test_admin_settings_page_saves_to_env(client, tmp_path, monkeypatch):
    from tests.test_web import admin_client
    env = tmp_path / ".env"
    env.write_text("GMAIL_APP_PASSWORD='abcd efgh'\nSWA_DAILY_AT=07:20\nCUSTOM=keep\n")
    monkeypatch.setenv("SWA_ENV_FILE", str(env))
    t = admin_client(client)
    page = client.get("/admin/settings").text
    assert "SWA_DAILY_PAGE_BUDGET" in page and "abcd efgh" not in page and "set; type to replace" in page
    form = {"csrf": t, "SWA_DAILY_AT": "06:12", "SWA_GPU": "1", "SWA_DAILY_PAGE_BUDGET": "15, 25",
            "GMAIL_APP_PASSWORD": "", "BASE_URL": "http://localhost:8080"}
    r = client.post("/admin/settings", data=form, follow_redirects=False)
    assert r.status_code == 303 and "Restart" in r.headers["location"]
    vals = envfile.read(env)
    assert vals["SWA_DAILY_AT"] == "06:12" and vals["SWA_GPU"] == "true" and vals["SWA_DAILY_PAGE_BUDGET"] == "15,25"
    assert vals["GMAIL_APP_PASSWORD"] == "abcd efgh" and vals["CUSTOM"] == "keep"   # secret kept, unknown kept
    assert "BASE_URL" not in vals and "# BASE_URL=" in env.read_text()            # default left commented
    client.post("/admin/settings", data={**form, "clear_GMAIL_APP_PASSWORD": "1"})
    assert envfile.read(env)["GMAIL_APP_PASSWORD"] == ""
    bad = client.post("/admin/settings", data={**form, "SESSION_DAYS": "two weeks"})
    assert "SESSION_DAYS: not a valid int" in bad.text


def test_admin_settings_need_an_admin_and_restart_is_deferred(client, monkeypatch):
    from app import db
    from tests.test_web import admin_client
    t = admin_client(client)
    started = []

    class Timer:
        def __init__(self, delay, fn): started.append(delay)
        def start(self): pass
    monkeypatch.setattr("app.web.threading.Timer", Timer)
    r = client.post("/admin/settings/restart", data={"csrf": t}, follow_redirects=False)
    assert r.status_code == 303 and started == [1.5]
    with db.tx() as con:
        con.execute("UPDATE users SET is_admin=0")
    assert client.get("/admin/settings", follow_redirects=False).status_code in (303, 403, 404)
