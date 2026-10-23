"""Audit log parser: valid lines, corrupt lines, display decisions, filter."""

import json

from desktop.audit_model import filter_entries, load_audit, parse_audit_lines

GOOD = [
    json.dumps({"ts": "2026-10-23T10:00:00+0500", "brain": "api", "tool": "screenshot",
                "args": "", "decision": "allowed"}),
    json.dumps({"ts": "2026-10-23T10:01:00+0500", "brain": "api", "tool": "delete_path",
                "args": "path=C:/x", "decision": "allowed", "confirmed": True}),
    json.dumps({"ts": "2026-10-23T10:02:00+0500", "brain": "sdk", "tool": "run_powershell",
                "args": "command=format", "decision": "denied", "confirmed": True}),
]


def test_parse_valid_lines():
    data = parse_audit_lines(GOOD)
    assert data.corrupt_lines == 0
    assert len(data.entries) == 3
    assert data.entries[0].tool == "screenshot"
    assert data.entries[0].display_decision == "allowed"
    assert data.entries[1].display_decision == "confirmed"
    assert data.entries[2].display_decision == "denied"


def test_corrupt_and_partial_lines_are_counted_not_fatal():
    lines = GOOD + ['{"ts": "2026-10-23T10:03', "not json", "[1,2]", ""]
    data = parse_audit_lines(lines)
    assert len(data.entries) == 3
    assert data.corrupt_lines == 3  # truncated json, junk, and a non-object


def test_load_missing_file_is_empty(tmp_path):
    data = load_audit(tmp_path / "audit.jsonl")
    assert data.entries == []
    assert data.corrupt_lines == 0


def test_load_file(tmp_path):
    path = tmp_path / "audit.jsonl"
    path.write_text("\n".join(GOOD) + "\n", encoding="utf-8")
    data = load_audit(path)
    assert len(data.entries) == 3


def test_filter_matches_tool_args_and_decision():
    entries = parse_audit_lines(GOOD).entries
    assert [e.tool for e in filter_entries(entries, "delete")] == ["delete_path"]
    assert [e.tool for e in filter_entries(entries, "DENIED")] == ["run_powershell"]
    assert len(filter_entries(entries, "")) == 3
    assert filter_entries(entries, "zzz") == []
