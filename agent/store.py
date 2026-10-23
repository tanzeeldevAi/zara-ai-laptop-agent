"""SQLite persistence: conversation history, owner facts, tasks, schedules.

One database file — `.runtime/memory.db` — backs four Phase 3 features:

  conversations   per-session message history (restored after a restart)
  facts           things the owner asked the assistant to remember
  tasks           the unified task queue (queued owner instructions)
  schedules       proactive / scheduled prompts (the scheduler enqueues them)

Schema versioning uses PRAGMA user_version with stepwise migrations.

Hard rule: NO SECRETS are ever stored here. API keys and tokens live in the
OS keyring / .env (agent/secrets.py, agent/config.py) — never in SQLite.
"""

import json
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA_VERSION = 1
HISTORY_CAP = 200          # messages kept per session; older ones are pruned

_V1_DDL = """
CREATE TABLE IF NOT EXISTS conversations(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_key TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conv_key ON conversations(session_key, id);
CREATE TABLE IF NOT EXISTS facts(
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_key TEXT NOT NULL,
    prompt TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    created_ts REAL NOT NULL,
    started_ts REAL,
    finished_ts REAL,
    result_summary TEXT NOT NULL DEFAULT '',
    cancel_requested INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, id);
CREATE TABLE IF NOT EXISTS schedules(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_key TEXT NOT NULL,
    prompt TEXT NOT NULL,
    kind TEXT NOT NULL,
    spec TEXT NOT NULL,
    next_run REAL NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    last_run REAL,
    created_ts REAL NOT NULL
);
"""

_META_DDL = """
CREATE TABLE IF NOT EXISTS meta(
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

TASK_STATUSES = ("queued", "running", "done", "failed", "cancelled")
TERMINAL_STATUSES = ("done", "failed", "cancelled")


class Store:
    """Thread-safe SQLite store. One instance per process (see get_store())."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._migrate()
            # Phase 4: the meta table is additive and deliberately NOT a
            # user_version bump — it is ensured on every open instead, so
            # the v1 schema contract above stays exactly as Phase 3 froze it.
            self._conn.executescript(_META_DDL)
            self._conn.commit()

    # ------------------------------------------------------------ schema ----

    def _migrate(self):
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if version < 1:
            self._conn.executescript(_V1_DDL)
            self._conn.execute("PRAGMA user_version = 1")
            self._conn.commit()
            version = 1
        # Future migrations append steps here (if version < 2: ... etc.)
        self.schema_version = version

    def close(self):
        with self._lock:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------- conversations --

    def append_message(self, session_key, message):
        """Persist one conversation message (a brain-shaped dict)."""
        key = str(session_key)
        try:
            payload = json.dumps(message, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return
        role = str(message.get("role", "")) if isinstance(message, dict) else ""
        with self._lock:
            self._conn.execute(
                "INSERT INTO conversations(session_key, role, content, ts) VALUES (?,?,?,?)",
                (key, role, payload, time.time()),
            )
            # Keep only the newest HISTORY_CAP messages for this session.
            self._conn.execute(
                "DELETE FROM conversations WHERE session_key = ? AND id NOT IN "
                "(SELECT id FROM conversations WHERE session_key = ? ORDER BY id DESC LIMIT ?)",
                (key, key, HISTORY_CAP),
            )
            self._conn.commit()

    def load_messages(self, session_key, limit=HISTORY_CAP):
        key = str(session_key)
        with self._lock:
            rows = self._conn.execute(
                "SELECT content FROM conversations WHERE session_key = ? "
                "ORDER BY id DESC LIMIT ?",
                (key, limit),
            ).fetchall()
        messages = []
        for row in reversed(rows):
            try:
                message = json.loads(row["content"])
            except (TypeError, ValueError):
                continue          # tolerate a corrupt row rather than lose the rest
            if isinstance(message, dict) and "role" in message:
                messages.append(message)
        return messages

    def clear_messages(self, session_key):
        with self._lock:
            self._conn.execute(
                "DELETE FROM conversations WHERE session_key = ?", (str(session_key),)
            )
            self._conn.commit()

    # ---------------------------------------------------------------- meta --

    def get_meta(self, key):
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key = ?", (str(key),)
            ).fetchone()
        return row["value"] if row else None

    def set_meta(self, key, value):
        with self._lock:
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(key), str(value)),
            )
            self._conn.commit()

    def delete_meta(self, key):
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM meta WHERE key = ?", (str(key),)
            )
            self._conn.commit()
            return cursor.rowcount > 0

    # --------------------------------------------------------------- facts --

    def set_fact(self, key, value):
        with self._lock:
            self._conn.execute(
                "INSERT INTO facts(key, value, updated_ts) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "updated_ts = excluded.updated_ts",
                (str(key), str(value), time.time()),
            )
            self._conn.commit()

    def get_fact(self, key):
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM facts WHERE key = ?", (str(key),)
            ).fetchone()
        return row["value"] if row else None

    def list_facts(self, query=None):
        with self._lock:
            if query:
                like = f"%{query}%"
                rows = self._conn.execute(
                    "SELECT key, value FROM facts WHERE key LIKE ? OR value LIKE ? "
                    "ORDER BY key",
                    (like, like),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT key, value FROM facts ORDER BY key"
                ).fetchall()
        return [(row["key"], row["value"]) for row in rows]

    def delete_fact(self, key):
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM facts WHERE key = ?", (str(key),)
            )
            self._conn.commit()
            return cursor.rowcount > 0

    # --------------------------------------------------------------- tasks --

    def create_task(self, session_key, prompt):
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO tasks(session_key, prompt, status, created_ts) "
                "VALUES (?,?, 'queued', ?)",
                (str(session_key), str(prompt), time.time()),
            )
            self._conn.commit()
            return cursor.lastrowid

    def get_task(self, task_id):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
        return dict(row) if row else None

    def count_ahead(self, task_id):
        """How many tasks would run before this one (queued or running, older)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM tasks WHERE id < ? AND status IN ('queued','running')",
                (task_id,),
            ).fetchone()
        return int(row["n"])

    def _claim_row(self, row):
        """Move a queued row to running — honouring a pre-set cancel flag."""
        if row["cancel_requested"]:
            self._conn.execute(
                "UPDATE tasks SET status = 'cancelled', finished_ts = ?, "
                "result_summary = 'Cancelled before it started.' WHERE id = ?",
                (time.time(), row["id"]),
            )
            self._conn.commit()
            return None
        self._conn.execute(
            "UPDATE tasks SET status = 'running', started_ts = ? WHERE id = ?",
            (time.time(), row["id"]),
        )
        self._conn.commit()
        return self.get_task(row["id"])

    def claim_next(self, exclude_ids=()):
        """Atomically claim the oldest queued task (global FIFO)."""
        with self._lock:
            while True:
                sql = "SELECT * FROM tasks WHERE status = 'queued'"
                params = []
                if exclude_ids:
                    marks = ",".join("?" for _ in exclude_ids)
                    sql += f" AND id NOT IN ({marks})"
                    params.extend(exclude_ids)
                sql += " ORDER BY id ASC LIMIT 1"
                row = self._conn.execute(sql, params).fetchone()
                if row is None:
                    return None
                claimed = self._claim_row(row)
                if claimed is not None:
                    return claimed
                # The row was cancelled-by-flag; loop to the next queued task.

    def claim_task(self, task_id):
        """Claim one specific queued task (inline / embedded execution mode)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE id = ? AND status = 'queued'", (task_id,)
            ).fetchone()
            if row is None:
                return None
            return self._claim_row(row)

    def set_task_status(self, task_id, status, result_summary=None):
        if status not in TASK_STATUSES:
            raise ValueError(f"unknown task status: {status}")
        with self._lock:
            if status in TERMINAL_STATUSES:
                self._conn.execute(
                    "UPDATE tasks SET status = ?, finished_ts = ?, "
                    "result_summary = COALESCE(?, result_summary) WHERE id = ?",
                    (status, time.time(), result_summary, task_id),
                )
            else:
                self._conn.execute(
                    "UPDATE tasks SET status = ? WHERE id = ?", (status, task_id)
                )
            self._conn.commit()

    def request_cancel(self, task_id):
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE tasks SET cancel_requested = 1 WHERE id = ? "
                "AND status IN ('queued','running')",
                (task_id,),
            )
            self._conn.commit()
            return cursor.rowcount > 0

    def list_tasks(self, session_key=None, statuses=None, limit=50):
        sql = "SELECT * FROM tasks"
        clauses, params = [], []
        if session_key is not None:
            clauses.append("session_key = ?")
            params.append(str(session_key))
        if statuses:
            marks = ",".join("?" for _ in statuses)
            clauses.append(f"status IN ({marks})")
            params.extend(statuses)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id ASC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def recover_running(self):
        """Crash recovery: tasks stranded in 'running' go back to 'queued'."""
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE tasks SET status = 'queued' WHERE status = 'running'"
            )
            self._conn.commit()
            return cursor.rowcount

    # ----------------------------------------------------------- schedules --

    def create_schedule(self, session_key, prompt, kind, spec, next_run):
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO schedules(session_key, prompt, kind, spec, next_run, created_ts) "
                "VALUES (?,?,?,?,?,?)",
                (
                    str(session_key),
                    str(prompt),
                    str(kind),
                    json.dumps(spec, ensure_ascii=False),
                    float(next_run),
                    time.time(),
                ),
            )
            self._conn.commit()
            return cursor.lastrowid

    def get_schedule(self, schedule_id):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM schedules WHERE id = ?", (schedule_id,)
            ).fetchone()
        return self._schedule_dict(row) if row else None

    def list_schedules(self, session_key=None, enabled_only=False):
        sql = "SELECT * FROM schedules"
        clauses, params = [], []
        if session_key is not None:
            clauses.append("session_key = ?")
            params.append(str(session_key))
        if enabled_only:
            clauses.append("enabled = 1")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY next_run ASC"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._schedule_dict(row) for row in rows]

    def due_schedules(self, now_ts):
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM schedules WHERE enabled = 1 AND next_run <= ? "
                "ORDER BY next_run ASC",
                (float(now_ts),),
            ).fetchall()
        return [self._schedule_dict(row) for row in rows]

    def update_schedule(self, schedule_id, **fields):
        allowed = {"next_run", "enabled", "last_run", "prompt", "kind"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return False
        assignments = ", ".join(f"{k} = ?" for k in updates)
        with self._lock:
            cursor = self._conn.execute(
                f"UPDATE schedules SET {assignments} WHERE id = ?",
                (*updates.values(), schedule_id),
            )
            self._conn.commit()
            return cursor.rowcount > 0

    def delete_schedule(self, schedule_id):
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM schedules WHERE id = ?", (schedule_id,)
            )
            self._conn.commit()
            return cursor.rowcount > 0

    @staticmethod
    def _schedule_dict(row):
        data = dict(row)
        try:
            data["spec"] = json.loads(data.get("spec") or "{}")
        except (TypeError, ValueError):
            data["spec"] = {}
        return data


# --------------------------------------------------------- history seam ----


class HistoryStore:
    """Interface the API brain uses for durable conversation history.

    The brain keeps its in-memory message list as the working copy; a
    HistoryStore is the backing that lets a restart pick the conversation
    back up. Implementations must never raise into a brain turn.
    """

    def load(self, key):  # -> list[dict]
        raise NotImplementedError

    def append(self, key, message):
        raise NotImplementedError

    def clear(self, key):
        raise NotImplementedError


class SqliteHistoryStore(HistoryStore):
    def __init__(self, store):
        self.store = store

    def load(self, key):
        try:
            return self.store.load_messages(str(key))
        except Exception:  # noqa: BLE001 - history must never break a turn
            return []

    def append(self, key, message):
        try:
            self.store.append_message(str(key), message)
        except Exception:  # noqa: BLE001
            pass

    def clear(self, key):
        try:
            self.store.clear_messages(str(key))
        except Exception:  # noqa: BLE001
            pass


# ------------------------------------------------------- default store -----

_default_store = None
_default_lock = threading.Lock()


def get_store():
    """The process-wide store at CONFIG.runtime_dir / 'memory.db' (lazy)."""
    global _default_store
    if _default_store is None:
        with _default_lock:
            if _default_store is None:
                from .config import CONFIG

                _default_store = Store(CONFIG.runtime_dir / "memory.db")
    return _default_store


def set_store(store):
    """Replace the process-wide store (tests, embedding)."""
    global _default_store
    with _default_lock:
        _default_store = store
    return store
