from app import applog, db
from tests.conftest import FakeSearcher
from tests.test_web import admin_client, activate, create_user, watch_form
from tests.test_worker import make_user, make_watch


# ---------- live progress ----------
def test_progress_is_reported_during_a_run(worker, sent, monkeypatch):
    uid = make_user()
    wid = make_watch(uid)
    seen = []
    orig = FakeSearcher.search

    def spy(self, *a):
        worker.running_user = uid  # as the worker loop sets it
        seen.append(worker.snapshot(uid))
        return orig(self, *a)

    monkeypatch.setattr(FakeSearcher, "search", spy)
    worker.run_user(uid)
    assert seen[0]["state"] == "running" and seen[0]["watch_id"] == wid
    assert seen[0]["total"] == 4 and [s["done"] for s in seen] == [1, 2, 3, 4]
    assert "searching" in seen[0]["phase"] and "(cash)" in seen[0]["phase"] or "(points)" in seen[0]["phase"]


def test_snapshot_idle_and_queued(worker):
    assert worker.snapshot(99) == {"state": None}
    worker.enqueue(99)
    assert worker.snapshot(99) == {"state": "queued"}


def test_error_mid_run_closes_the_run(worker, sent, monkeypatch):
    uid = make_user()
    wid = make_watch(uid)

    def boom(self, *a):
        raise RuntimeError("browser crashed")

    monkeypatch.setattr(FakeSearcher, "search", boom)
    try:
        worker.run_user(uid)
    except RuntimeError:
        pass
    run = db.one("SELECT * FROM runs WHERE watch_id=?", (wid,))
    assert run["finished"] and "browser crashed" in run["note"]
    w = db.one("SELECT * FROM watches WHERE id=?", (wid,))
    assert "browser crashed" in w["last_status"] and w["last_run_at"]


def test_browser_failing_to_start_is_shown_on_the_watch(worker, sent, monkeypatch):
    uid = make_user()
    wid = make_watch(uid)

    def no_browser(self):
        raise OSError("Chrome not installed")

    monkeypatch.setattr(FakeSearcher, "__enter__", no_browser)
    try:
        worker.run_user(uid)
    except OSError:
        pass
    assert "Chrome not installed" in db.one("SELECT last_status FROM watches WHERE id=?", (wid,))[0]


def test_interrupted_runs_are_closed_on_start(worker):
    uid = make_user()
    wid = make_watch(uid, last_status="Running…")
    with db.tx() as con:
        con.execute("INSERT INTO runs(watch_id, started) VALUES (?,?)", (wid, db.now()))
    worker.close_interrupted_runs()
    assert db.one("SELECT finished FROM runs WHERE watch_id=?", (wid,))[0]
    assert db.one("SELECT last_status FROM watches WHERE id=?", (wid,))[0].startswith("Interrupted")


def test_run_writes_log_entries(worker, sent):
    uid = make_user()
    wid = make_watch(uid)
    worker.run_user(uid)
    msgs = [r["message"] for r in db.query("SELECT * FROM logs WHERE user_id=? ORDER BY id", (uid,))]
    assert msgs[0].startswith("Starting a run") and msgs[-1].startswith("Run finished")
    assert any("Starting the browser" in m for m in msgs)
    assert any(m.endswith("flight(s), 1 matching") for m in msgs)
    assert db.one("SELECT COUNT(*) FROM logs WHERE watch_id=?", (wid,))[0] > 3


# ---------- web ----------
def test_status_api(client, worker):
    assert client.get("/api/status").status_code == 401
    admin_client(client)
    assert client.get("/api/status").json() == {"state": None}


def test_log_page_scoping_and_filters(client):
    token = admin_client(client)
    temp = create_user(client, token, "alice")
    alice = db.one("SELECT id FROM users WHERE username='alice'")["id"]
    applog.info("alice searched", user_id=alice)
    applog.error("alice error <script>x</script>", user_id=alice)
    applog.info("system thing")

    page = client.get("/log").text  # admin: everything
    assert "alice searched" in page and "system thing" in page and "<script>x" not in page
    page = client.get("/log?level=error").text
    assert "alice error" in page and "alice searched" not in page
    page = client.get("/log?user=system").text
    assert "system thing" in page and "alice searched" not in page
    assert "alice searched" in client.get("/log?q=searched").text

    activate(client, "alice", temp)
    page = client.get("/log").text  # alice: only her own
    assert "alice searched" in page and "system thing" not in page and "Created admin" not in page
    rows = client.get("/api/log?after=0").json()["rows"]
    assert {r["user_id"] for r in rows} == {alice}
    last = max(r["id"] for r in rows)
    assert client.get(f"/api/log?after={last}").json()["rows"] == []
    applog.info("new one", user_id=alice)
    assert [r["message"] for r in client.get(f"/api/log?after={last}").json()["rows"]] == ["new one"]


def test_dashboard_shows_running_watch(client, worker):
    token = admin_client(client)
    r = client.post("/watches", data=watch_form(token), follow_redirects=False)
    wid = int(r.headers["location"].split("/")[2].split("?")[0])
    uid = db.one("SELECT id FROM users WHERE username='admin'")["id"]
    worker.running_user = uid
    worker.phase(uid, "AUS→BWI: searching 2026-12-18 (cash)", watch_id=wid, done=1, total=4, log=False)
    page = client.get("/").text
    assert "Running now" in page and "searching 2026-12-18 (cash)" in page
    assert client.get("/api/status").json()["done"] == 1


def test_screenshots_attached_to_log_and_scoped(client, tmp_path):
    from app.config import settings
    token = admin_client(client)
    temp = create_user(client, token, "bob")
    bob = db.one("SELECT id FROM users WHERE username='bob'")["id"]
    shot = settings.debug_dir / "shots" / "x.png"
    shot.parent.mkdir(parents=True)
    shot.write_bytes(b"\\x89PNG fake")
    applog.warn("search failed", user_id=bob, attachment=shot)
    applog.warn("admin-only failure", attachment=shot)
    lid = db.one("SELECT id FROM logs WHERE message='search failed'")["id"]
    sys_id = db.one("SELECT id FROM logs WHERE message='admin-only failure'")["id"]
    assert db.one("SELECT attachment FROM logs WHERE id=?", (lid,))[0] == "debug/shots/x.png"

    assert f"/log/shot/{lid}" in client.get("/log").text  # admin sees it
    assert client.get(f"/log/shot/{lid}").headers["content-type"] == "image/png"

    activate(client, "bob", temp)
    rows = client.get("/api/log").json()["rows"]
    assert rows[0]["shot_url"] == f"/log/shot/{lid}" and "attachment" not in rows[0]
    assert client.get(f"/log/shot/{lid}").status_code == 200
    assert client.get(f"/log/shot/{sys_id}").status_code == 404  # not bob's


def test_attachment_outside_data_dir_is_refused(tmp_path):
    assert applog.attachment_path("../../etc/passwd") is None
    assert applog._rel("/etc/passwd") is None


def test_error_mid_run_logs_a_screenshot(worker, sent, monkeypatch, tmp_path):
    uid = make_user()
    make_watch(uid)
    shot = tmp_path / "debug" / "err.png"
    shot.parent.mkdir(parents=True, exist_ok=True)
    shot.write_bytes(b"png")
    monkeypatch.setattr(FakeSearcher, "capture", lambda self, tag: shot, raising=False)
    monkeypatch.setattr(FakeSearcher, "search", lambda self, *a: (_ for _ in ()).throw(RuntimeError("page died")))
    try:
        worker.run_user(uid)
    except RuntimeError:
        pass
    row = db.one("SELECT * FROM logs WHERE level='error' AND user_id=?", (uid,))
    assert "page died" in row["message"] and row["attachment"] == "debug/err.png"
    assert db.one("SELECT COUNT(*) FROM logs WHERE level='error'")[0] == 1  # logged once
