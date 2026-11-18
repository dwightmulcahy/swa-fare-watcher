from app import db, pagewatch


class FakePage:
    def __init__(self, present=(), structure=()):
        self.present, self.structure = set(present), list(structure)

    def locator(self, sel):
        page = self

        class L:
            def count(self):
                return 1 if sel in page.present else 0
        return L()

    def evaluate(self, js):
        return self.structure

    def content(self):
        return "<html>page</html>"


class FakeBrowser:
    def __init__(self, page, tmp):
        self.page, self.tmp = page, tmp

    def capture(self, tag):
        p = self.tmp / f"{tag}.png"
        p.write_bytes(b"png")
        return p


def _admin():
    db.init()
    with db.tx() as con:
        con.execute("INSERT INTO users(username, email, pw_hash, is_admin, must_change_pw, created_at) "
                    "VALUES ('boss','boss@example.com','x',1,0,'2026-01-01')")


def test_missing_elements_email_admins_once_with_screenshot_and_html(sent_with_files, tmp_path):
    _admin()
    b = FakeBrowser(FakePage(present={"#a"}), tmp_path)
    assert pagewatch.check_elements(b, "seat map", {"seats": "#a", "select": "#b"}) == ["select"]
    to, subject, body, files = sent_with_files[-1]
    assert to == "boss@example.com" and "seat map" in subject and "select" in body
    assert any(str(f).endswith(".png") for f in files) and any(str(f).endswith(".html") for f in files)
    pagewatch.check_elements(b, "seat map", {"seats": "#a", "select": "#b"})
    assert len(sent_with_files) == 1                                          # once per change


def test_structure_change_is_a_heads_up_after_a_baseline(sent_with_files, tmp_path):
    _admin()
    page = FakePage(structure=["form|lookup", "input|conf", "input|first"])
    b = FakeBrowser(page, tmp_path)
    assert pagewatch.check_structure(b, "lookup") is None and not sent_with_files   # first visit: baseline
    page.structure = ["form|lookup", "input|first"]                                 # once: not yet
    assert pagewatch.check_structure(b, "lookup") is None and not sent_with_files
    page.structure = ["form|lookup", "input|conf", "input|first"]                   # back: it was a blip
    assert pagewatch.check_structure(b, "lookup") is None
    page.structure = ["form|lookup", "input|confirmation", "input|first"]
    assert pagewatch.check_structure(b, "lookup") is None and not sent_with_files
    gone, new = pagewatch.check_structure(b, "lookup")                               # twice in a row
    assert gone == ["input|conf"] and new == ["input|confirmation"] and "structure changed" in sent_with_files[-1][1]
    assert pagewatch.check_structure(b, "lookup") is None                            # the new normal


def test_controls_that_only_appear_are_logged_not_emailed(sent_with_files, tmp_path):
    _admin()
    page = FakePage(structure=["form|lookup"])
    b = FakeBrowser(page, tmp_path)
    pagewatch.check_structure(b, "lookup")
    page.structure = ["form|lookup", "div|dialog"]
    pagewatch.check_structure(b, "lookup")
    assert pagewatch.check_structure(b, "lookup") == ([], ["div|dialog"]) and not sent_with_files


def test_flow_failures_group_by_kind_not_by_seat(sent_with_files, tmp_path):
    _admin()
    b = FakeBrowser(FakePage(), tmp_path)
    pagewatch.flow_failed("seat selection", RuntimeError("seat 3A didn't stick (shows 7A)"), b)
    pagewatch.flow_failed("seat selection", RuntimeError("seat 4A didn't stick (shows 8A)"), b)
    assert len(sent_with_files) == 1 and "didn't go as expected" in sent_with_files[0][1]


def test_off_switch(sent_with_files, tmp_path, monkeypatch):
    from app.config import settings
    _admin()
    monkeypatch.setattr(settings, "page_watch", False)
    assert pagewatch.check_elements(FakeBrowser(FakePage(), tmp_path), "x", {"a": "#a"}) == []
    assert not sent_with_files
