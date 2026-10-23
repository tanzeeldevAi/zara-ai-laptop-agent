"""Phase 3: the unified task queue — FIFO, notices, cancel, recovery."""

import asyncio
import threading

import pytest

import agent.queue as queue_mod
from agent.queue import LoopBoundChannel, TaskQueue
from agent.store import Store
from tests.fakes import FakeChannel


class FakeRunner:
    """Stands in for the session's brain turn; records execution order."""

    def __init__(self):
        self.ran = []
        self.gates = {}          # prompt -> threading.Event (set = let it finish)

    async def __call__(self, row, chan, live):
        self.ran.append(row["prompt"])
        gate = self.gates.get(row["prompt"])
        if gate is not None:
            while not gate.is_set():
                await asyncio.sleep(0.01)
        if chan is not None:
            await chan.send_text(f"done: {row['prompt']}")


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "memory.db")


@pytest.fixture
def runner():
    return FakeRunner()


def make_queue(store, runner, resolver=None):
    queue = TaskQueue(store, runner, channel_resolver=resolver)
    queue.start()
    return queue


async def test_fifo_ordering_across_sessions(store, runner):
    queue = make_queue(store, runner)
    chan = FakeChannel()
    try:
        ids = [
            queue.submit("chat-a", "first", chan=chan)[0],
            queue.submit("chat-b", "second", chan=chan)[0],
            queue.submit("chat-a", "third", chan=chan)[0],
        ]
        for task_id in ids:
            row = queue.wait_for(task_id, timeout=10)
            assert row["status"] == "done", row
        assert runner.ran == ["first", "second", "third"]   # global FIFO
        assert "done: second" in chan.texts                 # replies delivered
    finally:
        queue.stop()


async def test_submit_reports_position(store, runner):
    gate = threading.Event()
    runner.gates["slow"] = gate
    queue = make_queue(store, runner)
    chan = FakeChannel()
    try:
        first_id, ahead = queue.submit("chat", "slow", chan=chan)
        assert ahead == 0
        # wait until the first task is actually running
        for _ in range(200):
            if store.get_task(first_id)["status"] == "running":
                break
            await asyncio.sleep(0.02)
        second_id, ahead = queue.submit("chat", "next", chan=chan)
        assert ahead == 1                       # one task in front of it
        assert queue.position(second_id) == 2
        gate.set()
        assert queue.wait_for(second_id, timeout=10)["status"] == "done"
    finally:
        gate.set()
        queue.stop()


async def test_cancel_queued_task_never_runs(store, runner):
    gate = threading.Event()
    runner.gates["blocker"] = gate
    queue = make_queue(store, runner)
    chan = FakeChannel()
    try:
        blocker_id, _ = queue.submit("chat", "blocker", chan=chan)
        for _ in range(200):
            if store.get_task(blocker_id)["status"] == "running":
                break
            await asyncio.sleep(0.02)
        victim_id, _ = queue.submit("chat", "victim", chan=chan)

        assert queue.cancel(victim_id) == "cancelled"
        assert queue.cancel(99999) == "not_found"
        gate.set()
        assert queue.wait_for(blocker_id, timeout=10)["status"] == "done"
        assert store.get_task(victim_id)["status"] == "cancelled"
        assert "victim" not in runner.ran
        assert queue.cancel(victim_id) == "finished"
    finally:
        gate.set()
        queue.stop()


async def test_cancel_running_task(store, runner):
    gate = threading.Event()                   # never set: runs until cancelled
    runner.gates["long"] = gate
    queue = make_queue(store, runner)
    chan = FakeChannel()
    try:
        task_id, _ = queue.submit("chat", "long", chan=chan)
        for _ in range(200):
            if store.get_task(task_id)["status"] == "running":
                break
            await asyncio.sleep(0.02)
        assert queue.cancel(task_id) == "cancelling"
        row = queue.wait_for(task_id, timeout=10)
        assert row["status"] == "cancelled"
    finally:
        gate.set()
        queue.stop()


async def test_crash_recovery_running_back_to_queued(store, runner, tmp_path):
    # A previous process died mid-task: row stranded in 'running'.
    path = tmp_path / "memory.db"
    crashed = Store(path)
    task_id = crashed.create_task("chat", "leftover")
    crashed.claim_task(task_id)
    assert crashed.get_task(task_id)["status"] == "running"
    crashed.close()

    store2 = Store(path)
    chan = FakeChannel()
    queue = TaskQueue(store2, runner, channel_resolver=lambda key: chan)
    queue.start()
    try:
        row = queue.wait_for(task_id, timeout=10)
        assert row["status"] == "done"          # recovered, then executed
        assert runner.ran == ["leftover"]
    finally:
        queue.stop()


async def test_task_without_channel_is_deferred_not_failed(store, runner, monkeypatch):
    monkeypatch.setattr(queue_mod, "DEFER_RETRY_SECONDS", 0.3)
    reachable = {"chan": None}
    queue = TaskQueue(store, runner, channel_resolver=lambda key: reachable["chan"])
    queue.start()
    try:
        task_id, _ = queue.submit("chat", "proactive")   # no live channel
        await asyncio.sleep(0.8)
        row = store.get_task(task_id)
        assert row["status"] == "queued"        # waiting, not failed

        reachable["chan"] = FakeChannel()       # the owner reopens the chat
        row = queue.wait_for(task_id, timeout=10)
        assert row["status"] == "done"
        assert runner.ran == ["proactive"]
    finally:
        queue.stop()


async def test_failed_runner_marks_task_failed(store):
    async def bad_runner(row, chan, live):
        raise RuntimeError("boom")

    queue = TaskQueue(store, bad_runner)
    queue.start()
    try:
        task_id, _ = queue.submit("chat", "explode", chan=FakeChannel())
        row = queue.wait_for(task_id, timeout=10)
        assert row["status"] == "failed"
        assert "boom" in row["result_summary"]
        # ...and the worker is still alive for the next task
        assert queue.is_running
    finally:
        queue.stop()


# ------------------------------------------------------- LoopBoundChannel --

async def test_loop_bound_channel_marshals_to_home_loop():
    home_loop = asyncio.get_running_loop()
    raw = FakeChannel()
    bound = LoopBoundChannel(raw, home_loop)

    # Attribute delegation both ways (the safety pipeline sets .spoke).
    bound.spoke = True
    assert raw.spoke is True
    assert bound.user_id == raw.user_id

    # Use it from a DIFFERENT loop in another thread: sends still land.
    def foreign():
        async def go():
            await bound.send_text("from the worker loop")
            await bound.note("a note")

        asyncio.run(go())

    await asyncio.to_thread(foreign)
    assert "from the worker loop" in raw.texts
    assert raw.notes == ["a note"]
    assert bound.result_summary == "from the worker loop"

    # Same-loop use is direct.
    await bound.send_text("direct")
    assert raw.texts[-1] == "direct"
