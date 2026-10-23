"""Phase 3: conversation history survives a brain restart (API brain).

A fresh ApiBrain over the same store must load the earlier conversation —
that is the whole point of the SQLite backing. LiteLLM is mocked, exactly
as in test_transport_contract.py; no API calls are made.
"""

from types import SimpleNamespace

import pytest

from agent.brain.api_brain import ApiBrain, _sanitize_loaded
from agent.config import CONFIG
from agent.store import SqliteHistoryStore, Store
from tests.fakes import FakeChannel


def completion(content=""):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=None))])


@pytest.fixture
def history_store(tmp_path):
    return SqliteHistoryStore(Store(tmp_path / "memory.db"))


def make_brain(monkeypatch, history_store):
    monkeypatch.setattr(CONFIG, "api_provider", "anthropic")
    monkeypatch.setattr(CONFIG, "_provider_model_raw", "")
    monkeypatch.setattr(CONFIG, "model", "claude-opus-5")
    return ApiBrain(history_store=history_store)


async def test_history_restored_after_restart(monkeypatch, history_store):
    seen_messages = []

    async def fake_acompletion(**kwargs):
        seen_messages.append([dict(m) for m in kwargs["messages"]])
        return completion(content="reply")

    monkeypatch.setattr("agent.brain.api_brain.litellm.acompletion", fake_acompletion)

    brain1 = make_brain(monkeypatch, history_store)
    await brain1.handle("chat", "my name is Ahmed", FakeChannel())
    assert len(seen_messages) == 1

    # A brand-new brain (the "restart") over the same store:
    brain2 = make_brain(monkeypatch, history_store)
    await brain2.handle("chat", "what is my name?", FakeChannel())

    second_call = seen_messages[1]
    contents = [m.get("content") for m in second_call]
    assert "my name is Ahmed" in contents          # earlier turn was restored
    assert "reply" in contents                     # including the assistant's answer
    assert contents[-1] == "what is my name?"      # and the new turn is last


async def test_reset_clears_persisted_history(monkeypatch, history_store):
    async def fake_acompletion(**kwargs):
        return completion(content="ok")

    monkeypatch.setattr("agent.brain.api_brain.litellm.acompletion", fake_acompletion)

    brain = make_brain(monkeypatch, history_store)
    await brain.handle("chat", "hello", FakeChannel())
    assert history_store.load("chat") != []

    brain.reset("chat")
    assert history_store.load("chat") == []
    assert "chat" not in brain.sessions


async def test_image_messages_are_not_persisted(monkeypatch, history_store):
    brain = make_brain(monkeypatch, history_store)
    brain._persist("chat", {"role": "user", "content": "plain"})
    brain._persist(
        "chat",
        {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:..."}}]},
    )
    loaded = history_store.load("chat")
    assert [m["content"] for m in loaded] == ["plain"]


def test_sanitize_drops_dangling_tool_group():
    dangling = [
        {"role": "user", "content": "do it"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "t1", "type": "function", "function": {"name": "x", "arguments": "{}"}}],
        },
        # the tool result never landed (cancelled mid-turn)
    ]
    assert _sanitize_loaded(dangling) == [{"role": "user", "content": "do it"}]


def test_sanitize_keeps_complete_tool_group_and_drops_orphans():
    messages = [
        {"role": "tool", "tool_call_id": "orphan", "content": "stale"},
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "t1", "type": "function", "function": {"name": "x", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "t1", "content": "result"},
        {"role": "assistant", "content": "done"},
    ]
    cleaned = _sanitize_loaded(messages)
    assert [m["role"] for m in cleaned] == ["user", "assistant", "tool", "assistant"]
