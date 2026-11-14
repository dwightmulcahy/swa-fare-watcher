"""Read and write the .env file the container is started with (docker compose `env_file`).

The admin Settings page edits it; the file is rewritten whole, every setting under its group with
its explanation as a comment, so it stays easy to edit by hand too. Settings that aren't known
are kept at the end. The file is written in place (not replaced), because it's usually a single
file bind-mounted into the container and a rename would break that mount.
"""
import os
import re
from pathlib import Path

from .envspec import ALL, GROUPS

_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


def path():
    """The .env the admin page edits: SWA_ENV_FILE, else /config/.env (docker), else ./.env."""
    p = os.environ.get("SWA_ENV_FILE")
    if p:
        return Path(p)
    return Path("/config/.env") if Path("/config/.env").exists() else Path(".env")


def _unquote(v):
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] == "'":
        return v[1:-1]
    if len(v) >= 2 and v[0] == v[-1] == '"':
        return re.sub(r'\\(["\\$`])', r"\1", v[1:-1]).replace("\\n", "\n")
    return re.split(r"\s+#", v, maxsplit=1)[0].strip()   # unquoted: " # comment" ends the value


def parse(text):
    """KEY=VALUE pairs in file order (comments and blank lines skipped)."""
    out = {}
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        m = _LINE.match(line)
        if m:
            out[m.group(1)] = _unquote(m.group(2))
    return out


def read(p=None):
    p = p or path()
    try:
        return parse(Path(p).read_text())
    except FileNotFoundError:
        return {}


def quote(v):
    """A value as docker compose reads it back unchanged."""
    v = "" if v is None else str(v)
    if v == "" or re.fullmatch(r"[A-Za-z0-9_./:@+,\-]+", v):
        return v
    if "'" not in v:
        return f"'{v}'"                       # single quotes: taken literally, no $ expansion
    return '"' + v.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`") + '"'


def _comment(text, width=96):
    words, lines, cur = text.split(), [], "#"
    for w in words:
        if len(cur) + 1 + len(w) > width:
            lines.append(cur)
            cur = "#"
        cur += " " + w
    lines.append(cur)
    return "\n".join(lines)


def render(values):
    """The whole file: every known setting under its group, with its explanation and default.
    A setting not in `values` is written commented out at its default."""
    parts = ["# SWA Fare Watcher settings. Edited by the admin Settings page or by hand; the app reads",
             "# them when it starts (docker compose env_file), so restart it after changing them.", ""]
    for title, items in GROUPS:
        parts.append(f"# ---------- {title} ----------")
        for s in items:
            parts.append(_comment(f"{s.help} (default: {s.default or 'blank'})"))
            if s.name in values:
                parts.append(f"{s.name}={quote(values[s.name])}")
            else:
                parts.append(f"# {s.name}={quote(s.default)}")
        parts.append("")
    extra = {k: v for k, v in values.items() if k not in ALL}
    if extra:
        parts.append("# ---------- Other settings (not known to this version) ----------")
        parts += [f"{k}={quote(v)}" for k, v in extra.items()]
        parts.append("")
    return "\n".join(parts)


def write(values, p=None):
    """Rewrite the file in place with `values` (keeps the inode, so a bind mount stays intact)."""
    p = Path(p or path())
    text = render(values)
    with open(p, "r+" if p.exists() else "w") as f:
        f.seek(0)
        f.write(text)
        f.truncate()
    return p


def writable(p=None):
    p = Path(p or path())
    return os.access(p, os.W_OK) if p.exists() else os.access(p.parent, os.W_OK)
