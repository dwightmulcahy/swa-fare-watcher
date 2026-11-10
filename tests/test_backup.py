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
