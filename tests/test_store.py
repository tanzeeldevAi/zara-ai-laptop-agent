"""Phase 3: the SQLite store — schema, history, facts, tasks, schedules."""

import time

import pytest

from agent.store import HISTORY_CAP, Store


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "memory.db")


def test_schema_version_is_set(store):
    assert store.schema_version == 1
    version = store._conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == 1


def test_store_reopens_with_same_data(tmp_path):
    path = tmp_path / "memory.db"
    first = Store(path)
    first.set_fact("colour", "green")
    first.close()
    second = Store(path)
    assert second.get_fact("colour") == "green"


# ------------------------------------------------------------- history -----

def test_history_round_trip_and_clear(store):
    store.append_message("s1", {"role": "user", "content": "hello"})
    store.append_message("s1", {"role": "assistant", "content": "hi"})
    store.append_message("s2", {"role": "user", "content": "other chat"})

    loaded = store.load_messages("s1")
    assert [m["content"] for m in loaded] == ["hello", "hi"]
    assert [m["content"] for m in store.load_messages("s2")] == ["other chat"]

    store.clear_messages("s1")
    assert store.load_messages("s1") == []
    assert len(store.load_messages("s2")) == 1


def test_history_prunes_to_cap(store):
    for i in range(HISTORY_CAP + 10):
        store.append_message("s", {"role": "user", "content": f"m{i}"})
    loaded = store.load_messages("s", limit=HISTORY_CAP + 50)
    assert len(loaded) == HISTORY_CAP
    assert loaded[0]["content"] == "m10"          # oldest 10 pruned
    assert loaded[-1]["content"] == f"m{HISTORY_CAP + 9}"


def test_history_tolerates_corrupt_rows(store):
    store.append_message("s", {"role": "user", "content": "good"})
    store._conn.execute(
        "INSERT INTO conversations(session_key, role, content, ts) VALUES ('s','user','{broken',?)",
        (time.time(),),
    )
    store._conn.commit()
    loaded = store.load_messages("s")
    assert [m["content"] for m in loaded] == ["good"]


# --------------------------------------------------------------- facts -----

def test_facts_crud_and_search(store):
    store.set_fact("brother_name", "Ahmed")
    store.set_fact("office_address", "12 Main Street")
    store.set_fact("brother_name", "Ahmed Raza")       # overwrite

    assert store.get_fact("brother_name") == "Ahmed Raza"
    assert store.get_fact("missing") is None
    assert store.list_facts() == [
        ("brother_name", "Ahmed Raza"),
        ("office_address", "12 Main Street"),
    ]
    assert store.list_facts("office") == [("office_address", "12 Main Street")]
    assert store.list_facts("Ahmed") == [("brother_name", "Ahmed Raza")]

    assert store.delete_fact("brother_name") is True
    assert store.delete_fact("brother_name") is False
    assert store.get_fact("brother_name") is None


# --------------------------------------------------------------- tasks -----

def test_task_lifecycle_and_fifo_claim(store):
    first = store.create_task("s1", "one")
    second = store.create_task("s2", "two")
    third = store.create_task("s1", "three")

    assert store.count_ahead(first) == 0
    assert store.count_ahead(third) == 2

    claimed = store.claim_next()
    assert claimed["id"] == first and claimed["status"] == "running"
    store.set_task_status(first, "done", result_summary="ok")
    assert store.get_task(first)["result_summary"] == "ok"

    claimed = store.claim_next()
    assert claimed["id"] == second


def test_task_cancel_flag_prevents_claim(store):
    task_id = store.create_task("s", "never runs")
    assert store.request_cancel(task_id) is True
    assert store.claim_next() is None
    assert store.get_task(task_id)["status"] == "cancelled"


def test_recover_running_goes_back_to_queued(store):
    task_id = store.create_task("s", "interrupted")
    store.claim_task(task_id)
    assert store.get_task(task_id)["status"] == "running"
    assert store.recover_running() == 1
    assert store.get_task(task_id)["status"] == "queued"
    assert store.recover_running() == 0


def test_list_tasks_filters(store):
    store.create_task("a", "one")
    store.create_task("b", "two")
    rows = store.list_tasks(session_key="a")
    assert len(rows) == 1 and rows[0]["prompt"] == "one"
    assert len(store.list_tasks(statuses=("queued",))) == 2
    assert store.list_tasks(statuses=("done",)) == []


# ----------------------------------------------------------- schedules -----

def test_schedules_crud_and_due(store):
    now = time.time()
    sid = store.create_schedule(
        "chat", "morning briefing", "daily", {"time": "08:00"}, now - 10
    )
    future = store.create_schedule(
        "chat", "later", "daily", {"time": "09:00"}, now + 3600
    )
    row = store.get_schedule(sid)
    assert row["spec"] == {"time": "08:00"}
    assert row["kind"] == "daily"

    due = store.due_schedules(now)
    assert [r["id"] for r in due] == [sid]

    assert store.update_schedule(sid, next_run=now + 7200) is True
    assert store.due_schedules(now) == []

    assert store.delete_schedule(future) is True
    assert store.get_schedule(future) is None
    assert [r["id"] for r in store.list_schedules()] == [sid]
