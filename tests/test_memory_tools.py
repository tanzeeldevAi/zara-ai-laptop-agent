"""Phase 3: memory tools (remember / recall / forget) + their safety class."""

import pytest

from agent.safety import AuditLog, SafetyPipeline, ToolExecutor
from agent.security import classify
from agent.store import Store, set_store
from agent.tools import load_all
from tests.fakes import FakeChannel

_, HANDLERS = load_all()


@pytest.fixture
def store(tmp_path):
    store = Store(tmp_path / "memory.db")
    set_store(store)
    yield store
    set_store(None)


def test_remember_recall_forget_round_trip(store):
    out = HANDLERS["remember_fact"](key="wife_name", value="Fatima")
    assert "Remembered" in out["text"]
    assert store.get_fact("wife_name") == "Fatima"

    out = HANDLERS["recall_facts"]()
    assert "wife_name: Fatima" in out["text"]

    out = HANDLERS["recall_facts"](query="wife")
    assert "Fatima" in out["text"]
    out = HANDLERS["recall_facts"](query="nonexistent")
    assert "No stored facts match" in out["text"]

    out = HANDLERS["forget_fact"](key="wife_name")
    assert "Forgotten" in out["text"]
    assert store.get_fact("wife_name") is None

    out = HANDLERS["forget_fact"](key="wife_name")
    assert "nothing was deleted" in out["text"]


def test_remember_rejects_empty(store):
    out = HANDLERS["remember_fact"](key="  ", value="x")
    assert "needs both" in out["text"]
    assert store.list_facts() == []


def test_safety_classes():
    # Writing/reading memory is routine; deleting it is destructive.
    assert classify("remember_fact", {"key": "a", "value": "b"})[0] is False
    assert classify("recall_facts", {})[0] is False
    needs, reason = classify("forget_fact", {"key": "a"})
    assert needs is True and "memory" in reason


async def test_forget_fact_via_executor_asks_confirmation(store, tmp_path, monkeypatch):
    from agent.config import CONFIG

    monkeypatch.setattr(CONFIG, "confirm_risky", True)
    store.set_fact("old_fact", "gone soon")
    executor = ToolExecutor(
        SafetyPipeline(AuditLog(tmp_path / "audit.jsonl")), brain_label="test"
    )

    denied_chan = FakeChannel(confirm_answer=False)
    outcome = await executor.execute("forget_fact", {"key": "old_fact"}, denied_chan)
    assert outcome.is_error and "DENIED" in outcome.text
    assert store.get_fact("old_fact") == "gone soon"      # still there
    assert len(denied_chan.confirm_calls) == 1

    ok_chan = FakeChannel(confirm_answer=True)
    outcome = await executor.execute("forget_fact", {"key": "old_fact"}, ok_chan)
    assert not outcome.is_error
    assert store.get_fact("old_fact") is None
