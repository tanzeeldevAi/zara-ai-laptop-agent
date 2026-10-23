"""(d) The shared safety pipeline, on BOTH brain paths.

The regression this guards: before Phase 0 the subscription (SDK) brain never
called security.classify(), so CONFIRM_RISKY=true silently did nothing there.
Both brains now execute through the same ToolExecutor — these tests drive that
executor under both brain labels with a fake handler registry (nothing real
is executed) and a fake channel (no Discord).
"""

import json

import pytest

from agent.config import CONFIG
from agent.safety import AuditLog, SafetyPipeline, ToolExecutor
from tests.fakes import FakeChannel

DESTRUCTIVE = {"command": "Remove-Item C:\\data -Recurse -Force"}
BENIGN = {"command": "Get-Date"}


def make_executor(tmp_path, brain_label, calls):
    def fake_handler(**kwargs):
        calls.append(kwargs)
        return {"text": "done"}

    handlers = {
        "run_powershell": fake_handler,
        "delete_path": fake_handler,
    }
    audit = AuditLog(tmp_path / "audit.jsonl")
    return ToolExecutor(SafetyPipeline(audit), brain_label=brain_label, handlers=handlers)


@pytest.fixture(params=["api:anthropic", "subscription"])
def brain_label(request):
    return request.param


async def test_destructive_denied_when_confirm_on_and_owner_says_no(tmp_path, brain_label, monkeypatch):
    monkeypatch.setattr(CONFIG, "confirm_risky", True)
    calls = []
    executor = make_executor(tmp_path, brain_label, calls)
    chan = FakeChannel(confirm_answer=False)

    outcome = await executor.execute("run_powershell", DESTRUCTIVE, chan)

    assert outcome.is_error
    assert "DENIED" in outcome.text
    assert calls == []                      # handler never ran
    assert len(chan.confirm_calls) == 1     # owner WAS asked


async def test_destructive_runs_when_owner_approves(tmp_path, brain_label, monkeypatch):
    monkeypatch.setattr(CONFIG, "confirm_risky", True)
    calls = []
    executor = make_executor(tmp_path, brain_label, calls)
    chan = FakeChannel(confirm_answer=True)

    outcome = await executor.execute("run_powershell", DESTRUCTIVE, chan)

    assert not outcome.is_error
    assert outcome.text == "done"
    assert len(calls) == 1
    assert len(chan.confirm_calls) == 1


async def test_confirm_off_matches_original_full_autonomy(tmp_path, brain_label, monkeypatch):
    # Original behaviour with CONFIRM_RISKY=false: no prompt, immediate run —
    # even for an always-confirm tool like delete_path.
    monkeypatch.setattr(CONFIG, "confirm_risky", False)
    calls = []
    executor = make_executor(tmp_path, brain_label, calls)
    chan = FakeChannel(confirm_answer=False)

    outcome = await executor.execute("delete_path", {"path": "C:\\x"}, chan)

    assert not outcome.is_error
    assert len(calls) == 1
    assert chan.confirm_calls == []


async def test_always_confirm_tool_also_gated(tmp_path, brain_label, monkeypatch):
    monkeypatch.setattr(CONFIG, "confirm_risky", True)
    calls = []
    executor = make_executor(tmp_path, brain_label, calls)
    chan = FakeChannel(confirm_answer=False)

    outcome = await executor.execute("delete_path", {"path": "C:\\x"}, chan)

    assert outcome.is_error and "DENIED" in outcome.text
    assert calls == []


async def test_benign_command_never_prompts(tmp_path, brain_label, monkeypatch):
    monkeypatch.setattr(CONFIG, "confirm_risky", True)
    calls = []
    executor = make_executor(tmp_path, brain_label, calls)
    chan = FakeChannel(confirm_answer=False)

    outcome = await executor.execute("run_powershell", BENIGN, chan)

    assert not outcome.is_error
    assert len(calls) == 1
    assert chan.confirm_calls == []         # classify() said no confirmation needed


async def test_unknown_tool_reported_not_executed(tmp_path, brain_label):
    executor = make_executor(tmp_path, brain_label, [])
    outcome = await executor.execute("no_such_tool", {}, FakeChannel())
    assert outcome.is_error
    assert "Unknown tool" in outcome.text


async def test_audit_log_records_decisions(tmp_path, brain_label, monkeypatch):
    monkeypatch.setattr(CONFIG, "confirm_risky", True)
    executor = make_executor(tmp_path, brain_label, [])
    await executor.execute("run_powershell", DESTRUCTIVE, FakeChannel(confirm_answer=False))
    await executor.execute("run_powershell", BENIGN, FakeChannel())

    lines = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").strip().splitlines()
    entries = [json.loads(line) for line in lines]
    assert entries[0]["decision"] == "denied"
    assert entries[0]["tool"] == "run_powershell"
    assert entries[0]["brain"] == brain_label
    assert entries[1]["decision"] == "allowed"
