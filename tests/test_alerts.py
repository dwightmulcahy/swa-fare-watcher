from app import db
from app.worker import pick_alerts, record_alerted
from tests.conftest import future
from tests.test_worker import make_user, make_watch


def c(wid, cash, points, name="x"):
    return {"watch_id": wid, "row": {"cash": cash, "points": points}, "watch": name}


def test_first_search_emails_cheapest_then_only_better_deals():
    db.init()
    wid = make_watch(make_user())
    with db.tx() as con:
        first = pick_alerts(con, wid, [c(wid, 300, 25000), c(wid, 200, 18000), c(wid, 250, 15000)], limit=1)
        # cheapest cash, plus the cheapest-by-points one since it's a different fare
        assert [h["row"]["cash"] for h in first] == [200, 250]
        record_alerted(con, first)
    with db.tx() as con:
        assert pick_alerts(con, wid, [c(wid, 200, 18000), c(wid, 260, 16000)]) == []   # nothing better
        better = pick_alerts(con, wid, [c(wid, 199, 30000), c(wid, 400, 14000), c(wid, 205, 15000)])
        assert sorted(h["row"]["cash"] for h in better) == [199, 400]
        record_alerted(con, better)
        row = con.execute("SELECT cash, points FROM alerted WHERE watch_id=? AND key='__best__'", (wid,)).fetchone()
        assert (row["cash"], row["points"]) == (199, 14000)


def test_limit_caps_one_email():
    db.init()
    wid = make_watch(make_user())
    with db.tx() as con:
        out = pick_alerts(con, wid, [c(wid, 100 + i, None) for i in range(12)])
    assert [h["row"]["cash"] for h in out] == [100, 101, 102, 103, 104]


def test_worker_emails_once_until_cheaper(worker, sent):
    uid = make_user()
    make_watch(uid)
    assert worker.run_user(uid)["hits"] >= 1 and len(sent) == 1
    sent.clear()
    assert worker.run_user(uid)["hits"] == 0 and sent == []        # same fares: no email
    # a new date with the same fare isn't a better deal either
    with db.tx() as con:
        con.execute("UPDATE watches SET dates = dates || ?", ("," + future(15),))
    from tests.conftest import FakeSearcher
    FakeSearcher.calls = []
    assert worker.run_user(uid)["hits"] == 0 and sent == []
    assert any(call[2] == future(15) for call in FakeSearcher.calls)   # the new date was searched
