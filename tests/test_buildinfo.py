import json
import re

from app import buildinfo


def fake_git(tmp_path, head="ref: refs/heads/main", loose=None, packed=None,
             origin="git@github.com:dwightmulcahy/swa-fare-watcher.git"):
    g = tmp_path / "git"
    (g / "refs/heads").mkdir(parents=True)
    (g / "HEAD").write_text(head + "\n")
    if loose:
        (g / "refs/heads/main").write_text(loose + "\n")
    if packed:
        (g / "packed-refs").write_text(f"# pack-refs\n{packed} refs/heads/main\n")
    (g / "config").write_text(f'[core]\n\tbare = false\n[remote "origin"]\n\turl = {origin}\n')
    return str(g)


def test_release_build():
    info = buildinfo.compute({"APP_VERSION": "v1.4.2", "GIT_SHA": "ABCDEF1234567", "REPOSITORY": "o/r"}, git_dir="")
    assert (info["version"], info["commit"], info["repository"]) == ("1.4.2", "abcdef1", "https://github.com/o/r")
    assert buildinfo.label(info) == "v1.4.2"


def test_dev_build_reads_git(tmp_path):
    g = fake_git(tmp_path, loose="0123456789abcdef0123456789abcdef01234567")
    info = buildinfo.compute({}, git_dir=g)
    assert info["version"] == "dev" and info["commit"] == "0123456"
    assert info["repository"] == "https://github.com/dwightmulcahy/swa-fare-watcher"
    assert buildinfo.label(info) == "dev 0123456"


def test_packed_refs_and_https_origin(tmp_path):
    g = fake_git(tmp_path, packed="fedcba9876543210fedcba9876543210fedcba98",
                 origin="https://github.com/o/r")
    info = buildinfo.compute({}, git_dir=g)
    assert info["commit"] == "fedcba9" and info["repository"] == "https://github.com/o/r"


def test_bad_inputs_are_dropped(tmp_path):
    info = buildinfo.compute({"APP_VERSION": "latest", "GIT_SHA": "not-a-sha", "REPOSITORY": "a/b/c"}, git_dir="")
    assert (info["version"], info["commit"], info["repository"]) == ("dev", "", "")
    g = fake_git(tmp_path, head="ref: refs/../../etc/passwd")
    assert buildinfo.compute({}, git_dir=g)["commit"] == ""


def test_cli_writes_json(tmp_path):
    import os
    import subprocess
    import sys
    out = tmp_path / "build.json"
    env = {**os.environ, "APP_VERSION": "2.0.0", "GIT_SHA": "1234567", "GIT_DIR": ""}
    subprocess.run([sys.executable, "-m", "app.buildinfo", str(out)], env=env, check=True, capture_output=True)
    assert json.loads(out.read_text())["version"] == "2.0.0"


def test_pages_embed_build_info_and_about_endpoint(client):
    page = client.get("/login").text
    assert 'id="buildVersion"' in page and "/static/version.mjs" in page
    embedded = json.loads(re.search(r'id="build-info">(.*?)</script>', page, re.S).group(1))
    assert embedded["version"] and "commit" in embedded
    r = client.get("/api/about")
    assert r.headers["cache-control"] == "no-store" and r.json()["started_at"].endswith("Z")
    js = client.get("/static/about.mjs")
    assert js.headers["content-type"].startswith("text/javascript")
