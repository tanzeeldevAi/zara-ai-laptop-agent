"""Qt-free parser for the audit log (.runtime/audit.jsonl).

The SafetyPipeline appends one JSON object per tool call:
    {"ts": ..., "brain": ..., "tool": ..., "args": ..., "decision": ...,
     "confirmed": true/false (optional)}

The viewer needs a tolerant reader: the agent may be mid-write, and a line
damaged by a crash must not hide the rest of the log. Corrupt lines are
counted and skipped, never fatal.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class AuditEntry:
    ts: str = ""
    brain: str = ""
    tool: str = ""
    args: str = ""
    decision: str = ""          # raw value from the log
    confirmed: object = None    # True / False / None (key absent)
    raw: dict = field(default_factory=dict)

    @property
    def display_decision(self):
        """The three states the viewer shows: allowed / confirmed / denied —
        plus the pipeline's error states, passed through as-is."""
        if self.decision == "allowed":
            return "confirmed" if self.confirmed else "allowed"
        return self.decision or "?"


@dataclass
class AuditData:
    entries: list = field(default_factory=list)
    corrupt_lines: int = 0


def parse_audit_lines(lines):
    """Parse an iterable of raw lines into an AuditData (order preserved)."""
    data = AuditData()
    for line in lines:
        text = (line or "").strip()
        if not text:
            continue
        try:
            obj = json.loads(text)
            if not isinstance(obj, dict):
                raise ValueError("not an object")
        except (ValueError, TypeError):
            data.corrupt_lines += 1
            continue
        data.entries.append(
            AuditEntry(
                ts=str(obj.get("ts", "")),
                brain=str(obj.get("brain", "")),
                tool=str(obj.get("tool", "")),
                args=str(obj.get("args", "")),
                decision=str(obj.get("decision", "")),
                confirmed=obj.get("confirmed"),
                raw=obj,
            )
        )
    return data


def load_audit(path):
    """Load an audit.jsonl file; a missing file is an empty log, not an error."""
    path = Path(path)
    if not path.exists():
        return AuditData()
    return parse_audit_lines(path.read_text(encoding="utf-8", errors="replace").splitlines())


def filter_entries(entries, text):
    """Case-insensitive substring filter across the visible columns."""
    needle = (text or "").strip().lower()
    if not needle:
        return list(entries)
    return [
        entry
        for entry in entries
        if needle in entry.ts.lower()
        or needle in entry.brain.lower()
        or needle in entry.tool.lower()
        or needle in entry.args.lower()
        or needle in entry.display_decision.lower()
    ]
