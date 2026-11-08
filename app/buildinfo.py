"""Build metadata for the version label / About dialog (same scheme as repo-template's build.mjs).

  APP_VERSION  release version, e.g. 1.4.2 or v1.4.2 (release workflow); empty -> "dev"
  GIT_SHA      commit; if empty, read from GIT_DIR (compose.yaml passes .git as a build context)
  REPOSITORY   owner/repo on GitHub (release workflow); else GITHUB_REPOSITORY, else GIT_DIR's origin

The Docker build writes app/build.json with `python -m app.buildinfo app/build.json`.
Running from a checkout without build.json, the same values are read from the repo's .git.
"""
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BUILD_JSON = HERE / "build.json"


def _read(p):
    try:
        return Path(p).read_text().strip()
    except OSError:
        return ""


def git_head(git_dir):
    if not git_dir:
        return ""
    head = _read(Path(git_dir) / "HEAD")
    m = re.match(r"^ref: (refs/[^\s.][^\s]*)$", head)
    if not m:
        return head  # detached HEAD (or empty)
    ref = m.group(1)
    if ".." in ref:
        return ""
    loose = _read(Path(git_dir) / ref)
    if loose:
        return loose
    for line in _read(Path(git_dir) / "packed-refs").splitlines():
        if line.endswith(f" {ref}"):
            return line.split(" ")[0]
    return ""


def git_origin(git_dir):
    if not git_dir:
        return ""
    config = _read(Path(git_dir) / "config")
    section = next((s for s in re.split(r"^\[", config, flags=re.M) if s.startswith('remote "origin"]')), "")
    url = (re.search(r"^\s*url\s*=\s*(\S+)", section, re.M) or [None, ""])[1]
    m = re.search(r"github\.com[:/]([^/\s]+/[^/\s]+?)(?:\.git)?/?$", url or "")
    return m.group(1) if m else ""


def compute(env=None, git_dir=None, edition="web"):
    env = os.environ if env is None else env
    git_dir = git_dir if git_dir is not None else env.get("GIT_DIR")
    raw = (env.get("APP_VERSION") or "").lstrip("v")
    version = raw if re.fullmatch(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?", raw) else "dev"
    sha = env.get("GIT_SHA") or git_head(git_dir)
    commit = sha[:7].lower() if re.fullmatch(r"[0-9a-fA-F]{7,40}", sha or "") else ""
    repo = env.get("REPOSITORY") or env.get("GITHUB_REPOSITORY") or git_origin(git_dir)
    repository = f"https://github.com/{repo}" if re.fullmatch(r"[\w.-]+/[\w.-]+", repo or "") else ""
    return {"edition": edition, "version": version, "commit": commit, "repository": repository,
            "builtAt": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")}


_cached = None


def load():
    """build.json from the image; otherwise computed from this checkout's .git (a dev build)."""
    global _cached
    if _cached is None:
        try:
            _cached = json.loads(BUILD_JSON.read_text())
        except (OSError, ValueError):
            _cached = compute(env={}, git_dir=str(HERE.parent / ".git"))
            _cached["builtAt"] = ""
    return _cached


def label(info=None):
    info = info or load()
    return f"dev {info['commit']}".strip() if info["version"] == "dev" else f"v{info['version']}"


if __name__ == "__main__":
    out = Path(sys.argv[1] if len(sys.argv) > 1 else BUILD_JSON)
    info = compute()
    out.write_text(json.dumps(info, indent=2) + "\n")
    print(f"build info: {info['version']} {info['commit']} {info['repository']}".rstrip())
