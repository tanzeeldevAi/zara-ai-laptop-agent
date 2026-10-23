"""Phase 3: scheduler math, firing/catch-up, and the scheduler tools."""

import time
from datetime import datetime, timedelta

import pytest

from agent import scheduler as scheduler_mod
from agent.context import reset_current_session_key, set_current_session_key
from agent.safety import AuditLog, SafetyPipeline, ToolExecutor
from agent.scheduler import Scheduler, compute_next_run, parse_hhmm, parse_weekday
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


# ------------------------------------------------------------ next runs ----

def test_parse_helpers():
    assert parse_hhmm("08:30") == (8, 30)
    assert parse_hhmm("24:00") is None
    assert parse_hhmm("8:99") is None
    assert parse_hhmm("junk") is None
    assert parse_weekday(0) == 0 and parse_weekday(6) == 6
    assert parse_weekday("friday") == 4
    assert parse_weekday("Fri") == 4
    assert parse_weekday(7) is None


def test_daily_next_run():
    morning = datetime(2026, 10, 23, 7, 0)
    nxt = compute_next_run("daily", {"time": "08:00"}, from_dt=morning)
    assert nxt == datetime(2026, 10, 23, 8, 0)          # later today
    evening = datetime(2026, 10, 23, 21, 0)
    nxt = compute_next_run("daily", {"time": "08:00"}, from_dt=evening)
    assert nxt == datetime(2026, 10, 24, 8, 0)          # tomorrow


def test_weekly_next_run():
    friday = datetime(2026, 10, 23, 7, 0)               # 2026-10-23 is a Friday
    assert friday.weekday() == 4
    nxt = compute_next_run("weekly", {"time": "09:00", "weekday": 4}, from_dt=friday)
    assert nxt == datetime(2026, 10, 23, 9, 0)          # today, later
    nxt = compute_next_run("weekly", {"time": "06:00", "weekday": 4}, from_dt=friday)
    assert nxt == datetime(2026, 10, 30, 6, 0)          # next Friday
    nxt = compute_next_run("weekly", {"time": "06:00", "weekday": 0}, from_dt=friday)
    assert nxt == datetime(2026, 10, 26, 6, 0)          # Monday


def test_interval_and_once_next_run():
    now = datetime(2026, 10, 23, 12, 0)
    assert compute_next_run("interval", {"minutes": 45}, from_dt=now) == now + timedelta(minutes=45)
    assert compute_next_run("once", {"at": "2026-12-01 09:30"}, from_dt=now) == datetime(2026, 12, 1, 9, 30)
    with pytest.raises(ValueError):
        compute_next_run("interval", {"minutes": 0}, from_dt=now)
    with pytest.raises(ValueError):
        compute_next_run("daily", {"time": "99:99"}, from_dt=now)
    with pytest.raises(ValueError):
        compute_next_run("bogus", {}, from_dt=now)


# --------------------------------------------------------------- firing ----

class FakeQueue:
    def __init__(self):
        self.submitted = []

    def submit(self, session_key, prompt, chan=None, came_by_voice=False):
        self.submitted.append((session_key, prompt))
        return len(self.submitted), 0


def test_run_due_fires_once_and_advances(store):
    queue = FakeQueue()
    sched = Scheduler(store, queue)
    now = datetime.now()
    past = (now - timedelta(days=3)).timestamp()        # badly overdue
    sid = store.create_schedule("chat", "daily report", "daily", {"time": "08:00"}, past)
    future = store.create_schedule(
        "chat", "not yet", "daily", {"time": "08:00"}, (now + timedelta(hours=2)).timestamp()
    )

    fired = sched.run_due(now=now)
    assert fired == 1                                    # catch-up: exactly once
    assert queue.submitted == [("chat", "daily report")]

    row = store.get_schedule(sid)
    assert row["next_run"] > now.timestamp()             # advanced into the future
    assert row["last_run"] is not None
    assert store.get_schedule(future)["last_run"] is None

    # Running again immediately fires nothing new.
    assert sched.run_due(now=now) == 0


def test_run_due_once_disables(store):
    queue = FakeQueue()
    sched = Scheduler(store, queue)
    now = datetime.now()
    sid = store.create_schedule(
        "chat", "one shot", "once", {"at": "2020-01-01 00:00"}, (now - timedelta(minutes=5)).timestamp()
    )
    assert sched.run_due(now=now) == 1
    row = store.get_schedule(sid)
    assert row["enabled"] == 0
    assert sched.run_due(now=now) == 0                   # never again


# ---------------------------------------------------------------- tools ----

@pytest.fixture
def as_chat():
    token = set_current_session_key("fake:7")
    yield
    reset_current_session_key(token)


def test_schedule_tools_round_trip(store, as_chat):
    out = HANDLERS["schedule_task"](
        prompt="morning briefing", kind="daily", time="08:00"
    )
    assert "Scheduled as #" in out["text"]
    rows = store.list_schedules(session_key="fake:7")
    assert len(rows) == 1
    assert rows[0]["kind"] == "daily" and rows[0]["spec"]["time"] == "08:00"
    assert rows[0]["next_run"] > time.time()

    out = HANDLERS["list_schedules"]()
    assert "daily at 08:00" in out["text"]
    assert "morning briefing" in out["text"]

    out = HANDLERS["cancel_schedule"](id=rows[0]["id"])
    assert "cancelled" in out["text"]
    assert store.list_schedules() == []


def test_schedule_tool_validation(store, as_chat):
    out = HANDLERS["schedule_task"](prompt="x", kind="daily")       # no time
    assert "needs a time" in out["text"]
    out = HANDLERS["schedule_task"](prompt="x", kind="weekly", time="09:00")
    assert "needs a weekday" in out["text"]
    out = HANDLERS["schedule_task"](prompt="x", kind="once", at="2020-01-01 00:00")
    assert "in the past" in out["text"]
    out = HANDLERS["schedule_task"](prompt="x", kind="interval", minutes=30)
    assert "Scheduled as #" in out["text"]
    assert store.list_schedules()[0]["spec"]["minutes"] == 30


def test_schedule_tool_without_chat_context(store):
    out = HANDLERS["schedule_task"](prompt="x", kind="daily", time="08:00")
    assert "couldn't tell which chat" in out["text"]
    assert store.list_schedules() == []


async def test_schedule_tool_via_executor_gets_session_from_channel(store, tmp_path):
    """The executor must hand the tool the channel's session key (contextvar)."""
    executor = ToolExecutor(
        SafetyPipeline(AuditLog(tmp_path / "audit.jsonl")), brain_label="test"
    )
    chan = FakeChannel(user_id=1, channel_id=7)
    chan.session_key = "fake:7"
    outcome = await executor.execute(
        "schedule_task",
        {"prompt": "via executor", "kind": "interval", "minutes": 15},
        chan,
    )
    assert not outcome.is_error
    rows = store.list_schedules(session_key="fake:7")
    assert len(rows) == 1 and rows[0]["prompt"] == "via executor"
