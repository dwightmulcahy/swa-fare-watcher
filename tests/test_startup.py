from app import db, startup
from app.config import settings


def test_startup_check_reports_problems(monkeypatch, client):
    monkeypatch.setattr(settings, "gpu", False)
    monkeypatch.setattr(settings, "base_url", "http://localhost:8080")
    monkeypatch.setattr(settings, "gmail_user", "")
    monkeypatch.setattr(settings, "backup_copy_dir", "/nope/not-mounted")
    res = {n: (ok, m) for n, ok, m in startup.run()}
    assert res["GPU"][0] is False and "SWA_GPU" in res["GPU"][1]
    assert res["Links"][0] is False and res["Email"][0] is False
    assert res["Backups"][0] is False and "not mounted" in res["Backups"][1]
    assert res["Storage"][0] is True and res["Version"][0] is None
    logs = db.query("SELECT level, message FROM logs WHERE source='startup'")
    assert any(r["level"] == "warn" and r["message"].startswith("GPU:") for r in logs)
    assert any("Start-up check:" in r["message"] and "GPU" in r["message"] for r in logs)


def test_startup_check_on_admin_page(client, monkeypatch, tmp_path):
    from tests.test_web import admin_client
    dev = tmp_path / "renderD128"
    dev.write_text("")
    monkeypatch.setattr(settings, "gpu", True)
    monkeypatch.setattr(settings, "gpu_device", str(dev))
    startup.run(log=False)
    admin_client(client)
    page = client.get("/admin/health").text
    assert "Start-up check" in page and "SWA_GPU on" in page
