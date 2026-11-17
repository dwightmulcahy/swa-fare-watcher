from app import db


def test_backup_daily_copy_and_pruning(tmp_path):
    import sqlite3
    from app.config import settings
    with db.tx() as con:
        con.execute("INSERT INTO users(username, pw_hash, created_at) VALUES ('bk', 'x', ?)", (db.now(),))
    first = db.backup(keep=2, day="2027-01-01")
    assert first and first.name == "app-2027-01-01.db"
    assert db.backup(keep=2, day="2027-01-01") is None          # once a day
    con = sqlite3.connect(first)
    assert con.execute("SELECT username FROM users WHERE username='bk'").fetchone()
    con.close()
    db.backup(keep=2, day="2027-01-02")
    db.backup(keep=2, day="2027-01-03")
    names = sorted(p.name for p in (settings.data_dir / "backups").iterdir())
    assert names == ["app-2027-01-02.db", "app-2027-01-03.db"]
    assert oct(first.with_name("app-2027-01-03.db").stat().st_mode & 0o777) == "0o600"
    assert db.backup(keep=0) is None


def test_backup_copies_to_a_second_folder(tmp_path, monkeypatch):
    import pytest
    from app import db
    from app.config import settings
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    db.init()
    for d in ("2027-01-01", "2027-01-02", "2027-01-03"):
        db.backup(keep=5, day=d)
    with pytest.raises(FileNotFoundError, match="mounted"):
        db.copy_backups(tmp_path / "usb", keep=2)          # unmounted: never created inside the container
    (tmp_path / "usb").mkdir()
    got = db.copy_backups(tmp_path / "usb", keep=2)
    assert [f.name for f in got] == ["app-2027-01-02.db", "app-2027-01-03.db"]
    assert db.copy_backups(tmp_path / "usb", keep=2) == []                                   # already there
    db.backup(keep=5, day="2027-01-04")
    assert [f.name for f in db.copy_backups(tmp_path / "usb", keep=2)] == ["app-2027-01-04.db"]
    assert sorted(f.name for f in (tmp_path / "usb").iterdir()) == ["app-2027-01-03.db", "app-2027-01-04.db"]
    assert db.copy_backups("", keep=2) == []
