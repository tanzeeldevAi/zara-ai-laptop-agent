"""Phase 3: session + queue integration — busy means QUEUED, not rejected.

Drives AgentSession with its real worker thread started (as in main.py)
and a stub brain. The old "I'm still working on the previous instruction"
rejection must be gone: a second instruction lines up, the owner is told
its position, and /queue + /cancel manage the line.
"""

import asyncio
import threading

import pytest

from agent.session import AgentSession
from agent.store import Store
from agent.transport import IncomingMessage
from tests.fakes import FakeChannel


class StubBrain:
    def __init__(self):
        self.handled = []
        self.gates = {}          # text -> threading.Event (unset = blocked)

    async def handle(self, key, text, chan):
        self.handled.append((key, text))
        gate = self.gates.get(text)
        if gate is not None:
            while not gate.is_set():
                await asyncio.sleep(0.01)
        await chan.send_text(f"done: {text}")

    def reset(self, key):
        pass


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "memory.db")


def make_chan():
    chan = FakeChannel(user_id=1, channel_id=7)
    chan.session_key = "fake:7"
    return chan


async def wait_until(predicate, timeout=10.0):
    for _ in range(int(timeout / 0.02)):
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


async def test_busy_session_queues_with_notice(store):
    brain = StubBrain()
    gate = threading.Event()
    brain.gates["first"] = gate
    session = AgentSession(brain, "stub", store=store)
    session.queue.start()
    chan = make_chan()
    try:
        await session.handle_message(IncomingMessage(channel=chan, text="first"))
        assert await wait_until(
            lambda: any(r["status"] == "running" for r in store.list_tasks())
        )

        await session.handle_message(IncomingMessage(channel=chan, text="second"))
        # The second instruction was NOT rejected — it queued, with a notice.
        assert any("position 2" in t for t in chan.texts)
        rows = store.list_tasks(statuses=("queued",))
        assert len(rows) == 1 and rows[0]["prompt"] == "second"

        gate.set()
        assert await wait_until(
            lambda: all(
                r["status"] == "done" for r in store.list_tasks()
            )
        )
        assert [text for _, text in brain.handled] == ["first", "second"]
        assert "done: second" in chan.texts
        # The queue stored each task's last reply as its result summary.
        summaries = {r["prompt"]: r["result_summary"] for r in store.list_tasks()}
        assert summaries["first"] == "done: first"
        assert summaries["second"] == "done: second"
    finally:
        gate.set()
        session.queue.stop()


async def test_queue_and_cancel_commands(store):
    brain = StubBrain()
    gate = threading.Event()
    brain.gates["blocker"] = gate
    session = AgentSession(brain, "stub", store=store)
    session.queue.start()
    chan = make_chan()
    try:
        await session.handle_message(IncomingMessage(channel=chan, text="blocker"))
        assert await wait_until(
            lambda: any(r["status"] == "running" for r in store.list_tasks())
        )
        await session.handle_message(IncomingMessage(channel=chan, text="later"))
        queued = store.list_tasks(statuses=("queued",))
        assert len(queued) == 1
        task_id = queued[0]["id"]

        await session.handle_message(IncomingMessage(channel=chan, text="/queue"))
        assert any(f"#{task_id}" in t and "position 2" in t for t in chan.texts)

        await session.handle_message(
            IncomingMessage(channel=chan, text=f"/cancel {task_id}")
        )
        assert any("cancelled" in t for t in chan.texts)
        assert store.get_task(task_id)["status"] == "cancelled"

        gate.set()
        assert await wait_until(
            lambda: store.list_tasks(statuses=("running", "queued")) == []
        )
        assert [text for _, text in brain.handled] == ["blocker"]   # 'later' never ran
    finally:
        gate.set()
        session.queue.stop()


async def test_stop_cancels_running_task(store):
    brain = StubBrain()
    gate = threading.Event()           # never set: only /stop ends it
    brain.gates["long job"] = gate
    session = AgentSession(brain, "stub", store=store)
    session.queue.start()
    chan = make_chan()
    try:
        await session.handle_message(IncomingMessage(channel=chan, text="long job"))
        assert await wait_until(
            lambda: any(r["status"] == "running" for r in store.list_tasks())
        )
        await session.handle_message(IncomingMessage(channel=chan, text="/stop"))
        assert any(t == "Stopping." for t in chan.texts)
        assert await wait_until(
            lambda: store.list_tasks()[0]["status"] == "cancelled"
        )
    finally:
        gate.set()
        session.queue.stop()


async def test_cancel_rejects_other_chats_task(store):
    brain = StubBrain()
    session = AgentSession(brain, "stub", store=store)
    session.queue.start()
    chan = make_chan()
    other_task = store.create_task("fake:99", "not yours")
    try:
        await session.handle_message(
            IncomingMessage(channel=chan, text=f"/cancel {other_task}")
        )
        assert any("isn't in this chat" in t for t in chan.texts)
        # Refused: not cancelled (the worker may be mid-deferral with it).
        assert store.get_task(other_task)["status"] in ("queued", "running")
    finally:
        session.queue.stop()
