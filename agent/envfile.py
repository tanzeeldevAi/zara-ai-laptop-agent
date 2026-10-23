"""A careful .env reader/writer for the desktop GUI (Phase 2).

python-dotenv reads .env fine, but the setup wizard and settings window also
need to *write* it — without destroying the file the user may have edited by
hand. EnvFile keeps the original lines (comments, blank lines, ordering,
unknown keys) and only rewrites the lines whose values actually change.

Values are stored plainly: a value is written unquoted when it is safe
(no spaces, '#', quotes or newlines) and double-quoted otherwise. Reading
understands single-quoted, double-quoted (with \\n / \\" / \\\\ escapes) and
bare values, plus an optional `export` prefix — the same subset the agent's
own config loader (python-dotenv) accepts for this project's keys.
"""

import re
from pathlib import Path

_KV_RE = re.compile(r"^(?P<indent>\s*)(?:export\s+)?(?P<key>[A-Za-z_][A-Za-z0-9_.]*)\s*=(?P<rest>.*)$")
_SAFE_VALUE_RE = re.compile(r"^[A-Za-z0-9_./:@%+\-,;=]*$")


def _parse_value(raw):
    """Decode the right-hand side of a KEY=... line into a plain string."""
    text = raw.strip()
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        body = text[1:-1]
        out = []
        i = 0
        while i < len(body):
            ch = body[i]
            if ch == "\\" and i + 1 < len(body):
                nxt = body[i + 1]
                out.append({"n": "\n", "t": "\t", '"': '"', "\\": "\\"}.get(nxt, nxt))
                i += 2
            else:
                out.append(ch)
                i += 1
        return "".join(out)
    if len(text) >= 2 and text[0] == "'" and text[-1] == "'":
        return text[1:-1]
    # Bare value: a trailing comment starts at ' #' (dotenv semantics).
    if " #" in text:
        text = text.split(" #", 1)[0].rstrip()
    return text


def _format_value(value):
    """Encode a plain string for the right-hand side of a KEY=... line."""
    value = "" if value is None else str(value)
    if value == "":
        return ""
    if _SAFE_VALUE_RE.match(value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


class _Entry:
    __slots__ = ("kind", "raw", "key", "value")

    def __init__(self, kind, raw, key=None, value=None):
        self.kind = kind          # "raw" | "kv"
        self.raw = raw            # original line (no newline)
        self.key = key
        self.value = value


class EnvFile:
    """An ordered, comment-preserving view of one .env file."""

    def __init__(self, path, entries=None, existed=True):
        self.path = Path(path)
        self.entries = entries or []
        self.existed = existed

    # ------------------------------------------------------------- loading

    @classmethod
    def load(cls, path):
        """Load a .env file. A missing file yields an empty EnvFile."""
        path = Path(path)
        if not path.exists():
            return cls(path, [], existed=False)
        text = path.read_text(encoding="utf-8")
        return cls(path, cls._parse(text), existed=True)

    @classmethod
    def bootstrap(cls, path, example_path):
        """Load `path`, creating it from `example_path` first if missing.

        Returns (envfile, created_flag)."""
        path = Path(path)
        if path.exists():
            return cls.load(path), False
        example = Path(example_path)
        if example.exists():
            path.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
        else:
            path.write_text("", encoding="utf-8")
        return cls.load(path), True

    @staticmethod
    def _parse(text):
        entries = []
        for line in text.splitlines():
            match = _KV_RE.match(line)
            if match and not line.lstrip().startswith("#"):
                entries.append(_Entry("kv", line, match.group("key"), _parse_value(match.group("rest"))))
            else:
                entries.append(_Entry("raw", line))
        return entries

    # ------------------------------------------------------------- access

    def get(self, key, default=None):
        """Last value wins (dotenv semantics for duplicate keys)."""
        found = default
        for entry in self.entries:
            if entry.kind == "kv" and entry.key == key:
                found = entry.value
        return found

    def keys(self):
        return [e.key for e in self.entries if e.kind == "kv"]

    def set(self, key, value):
        """Update the (last) existing line for `key`, or append a new one.

        Comments and ordering elsewhere in the file are untouched."""
        value = "" if value is None else str(value)
        target = None
        for entry in self.entries:
            if entry.kind == "kv" and entry.key == key:
                target = entry
        rendered = f"{key}={_format_value(value)}"
        if target is None:
            self.entries.append(_Entry("kv", rendered, key, value))
        else:
            target.raw = rendered
            target.value = value

    # -------------------------------------------------------------- saving

    def render(self):
        lines = [e.raw for e in self.entries]
        return "\n".join(lines) + ("\n" if lines else "")

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(self.render(), encoding="utf-8")
        tmp.replace(self.path)
        self.existed = True
