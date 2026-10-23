"""Unified task queue: every owner instruction becomes a durable task.

Phase 3 replaces the old behaviour — one job per session, and a flat
"I'm still working on the previous instruction" rejection when busy — with
a single global FIFO queue backed by the SQLite store:

  - session.py submits each instruction as a task instead of running it
    inline; a worker thread (its own event loop) claims tasks oldest-first
    across ALL sessions and transports and runs them one at a time.
  - Results are delivered as they are produced, through the originating
    session's channel — see LoopBoundChannel for how a channel that belongs
    to a transport's event loop is safely used from the worker loop.
  - Tasks whose chat has not been seen since startup (crash-recovered
    tasks, scheduler tasks for a chat the owner hasn't reopened yet) are
    DEFERRED, not failed: they run as soon as that session's channel
    registers again.
  - Crash recovery: tasks stranded in 'running' by a dead process are put
    back to 'queued' when the worker starts.

Cancellation is cooperative in the same sense /stop always was: the
running task's asyncio task is cancelled, which lands at the brain's next
await point (between tool calls). A tool call already executing in a
worker thread runs to completion and its result is discarded.
"""

import asyncio
import contextlib
import threading
import time

DEFER_RETRY_SECONDS = 30
SUMMARY_LIMIT = 500


class LoopBoundChannel:
    """A channel proxy that is safe to use from any event loop.

    Transports build channel objects on their own event loop (Discord's,
    Telegram's, the WhatsApp bridge loop) and some of their machinery
    (Discord's confirm view, asyncio futures) is bound to that loop. The
    queue worker runs on a different loop, so every coroutine this proxy is
    asked to run is marshalled back onto the channel's home loop with
    run_coroutine_threadsafe. Called FROM the home loop (the session's
    inline fallback), it delegates directly — zero behaviour change.

    Plain attribute reads/writes (user_id, last_spoken, spoke...) delegate
    to the wrapped channel. Outgoing texts are recorded so the queue can
    store a result summary when the task finishes.
    """

    _LOCAL_ATTRS = ("_chan", "_loop", "sent_texts")

    def __init__(self, chan, home_loop):
        object.__setattr__(self, "_chan", chan)
        object.__setattr__(self, "_loop", home_loop)
        object.__setattr__(self, "sent_texts", [])

    # -- attribute delegation ---------------------------------------------

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_chan"), name)

    def __setattr__(self, name, value):
        if name in LoopBoundChannel._LOCAL_ATTRS:
            object.__setattr__(self, name, value)
        else:
            setattr(object.__getattribute__(self, "_chan"), name, value)

    @property
    def wrapped(self):
        return object.__getattribute__(self, "_chan")

    @property
    def result_summary(self):
        for text in reversed(self.sent_texts):
            stripped = str(text).strip()
            if stripped:
                return stripped[:SUMMARY_LIMIT]
        return ""

    # -- loop marshalling ---------------------------------------------------

    def _on_home_loop(self):
        try:
            return asyncio.get_running_loop() is self._loop
        except RuntimeError:
            return False

    async def _dispatch(self, coro):
        if self._on_home_loop():
            return await coro
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return await asyncio.wrap_future(future)

    # -- channel contract ---------------------------------------------------

    async def send_text(self, text):
        self.sent_texts.append(str(text))
        return await self._dispatch(self._chan.send_text(text))

    async def note(self, text):
        return await self._dispatch(self._chan.note(text))

    async def send_file(self, path, caption=None):
        return await self._dispatch(self._chan.send_file(path, caption))

    async def send_voice(self, path, caption=None):
        send = getattr(self._chan, "send_voice", None) or self._chan.send_file
        return await self._dispatch(send(path, caption))

    async def confirm(self, tool_name, reason, detail):
        return await self._dispatch(self._chan.confirm(tool_name, reason, detail))

    def typing(self):
        # The typing indicator is cosmetic; only drive it on the home loop,
        # where the wrapped channel's context manager is valid.
        if self._on_home_loop() and hasattr(self._chan, "typing"):
            return self._chan.typing()
        return contextlib.nullcontext()


class _LiveTask:
    """In-memory delivery info for a task submitted by a live message."""

    __slots__ = ("chan", "came_by_voice")

    def __init__(self, chan, came_by_voice=False):
        self.chan = chan
        self.came_by_voice = came_by_voice


class TaskQueue:
    """Store-backed FIFO queue + single worker thread.

    `runner` is an async callable(task_row, chan, live) that executes one
    task (the session's brain turn). `channel_resolver` maps a session_key
    to a LoopBoundChannel for tasks that arrive without a live channel
    (scheduler tasks, crash-recovered tasks); returning None defers them.
    """

    def __init__(self, store, runner, channel_resolver=None):
        self.store = store
        self.runner = runner
        self.channel_resolver = channel_resolver
        self._live = {}                     # task_id -> _LiveTask
        self._live_lock = threading.Lock()
        self._deferred = {}                 # task_id -> monotonic retry ts
        self._thread = None
        self._loop = None
        self._wake = None
        self._current = None                # asyncio.Task executing now
        self._current_id = None
        self._stopped = False

    # ------------------------------------------------------------ submit ---

    def submit(self, session_key, prompt, chan=None, came_by_voice=False):
        """Queue one instruction. Returns (task_id, tasks_ahead)."""
        task_id = self.store.create_task(session_key, prompt)
        if chan is not None:
            with self._live_lock:
                self._live[task_id] = _LiveTask(chan, came_by_voice)
        ahead = self.store.count_ahead(task_id)
        self._notify()
        return task_id, ahead

    def position(self, task_id):
        """1-based place in line (1 = runs next / is running)."""
        return self.store.count_ahead(task_id) + 1

    # ------------------------------------------------------------ worker ---

    @property
    def is_running(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self):
        """Crash recovery + worker thread. Safe to call once."""
        if self._thread is not None:
            return
        recovered = self.store.recover_running()
        if recovered:
            print(f"[queue] recovered {recovered} interrupted task(s) back to the queue")
        self._stopped = False
        self._thread = threading.Thread(
            target=self._thread_main, name="task-queue", daemon=True
        )
        self._thread.start()

    def stop(self):
        self._stopped = True
        self._notify()

    def _thread_main(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._wake = asyncio.Event()
        try:
            self._loop.run_until_complete(self._worker())
        finally:
            self._loop.close()

    def _notify(self):
        loop, wake = self._loop, self._wake
        if loop is not None and wake is not None and loop.is_running():
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(wake.set)

    async def _worker(self):
        while not self._stopped:
            now = time.monotonic()
            deferred = [
                task_id for task_id, retry in self._deferred.items() if retry > now
            ]
            row = self.store.claim_next(exclude_ids=deferred)
            if row is None:
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), timeout=1.0)
                self._wake.clear()
                continue
            await self._execute(row)

    # ----------------------------------------------------------- execute ---

    async def _execute(self, row):
        task_id = row["id"]
        with self._live_lock:
            live = self._live.pop(task_id, None)
        chan = live.chan if live is not None else None
        if chan is None and self.channel_resolver is not None:
            chan = self.channel_resolver(row["session_key"])
        if chan is None:
            # Nobody to deliver to yet (chat not seen since startup): put
            # the task back and retry later instead of failing it.
            self.store.set_task_status(task_id, "queued")
            self._deferred[task_id] = time.monotonic() + DEFER_RETRY_SECONDS
            return

        self._deferred.pop(task_id, None)
        self._current_id = task_id
        self._current = asyncio.create_task(self.runner(row, chan, live))
        try:
            await self._current
            summary = getattr(chan, "result_summary", "") or ""
            self.store.set_task_status(task_id, "done", result_summary=summary)
        except asyncio.CancelledError:
            self.store.set_task_status(
                task_id, "cancelled", result_summary="Cancelled by the owner."
            )
        except Exception as exc:  # noqa: BLE001 - one bad task must not kill the worker
            self.store.set_task_status(
                task_id, "failed",
                result_summary=f"{type(exc).__name__}: {exc}"[:SUMMARY_LIMIT],
            )
        finally:
            self._current = None
            self._current_id = None

    async def run_inline(self, task_id):
        """Execute one submitted task on the CALLER's loop.

        Fallback for when the worker thread was never started (tests,
        embedding): the session awaits this where it used to run the brain
        directly, so behaviour is identical — including /stop, which cancels
        the same execution task either way.
        """
        row = self.store.claim_task(task_id)
        if row is None:
            return
        await self._execute(row)

    # ------------------------------------------------------------ cancel ---

    def cancel(self, task_id):
        """Cancel a task. Returns one of:
        'cancelled' (was queued), 'cancelling' (running; cancel signalled),
        'finished' (already terminal), 'not_found'.
        """
        row = self.store.get_task(task_id)
        if row is None:
            return "not_found"
        status = row["status"]
        if status in ("done", "failed", "cancelled"):
            return "finished"
        self.store.request_cancel(task_id)
        if status == "queued":
            with self._live_lock:
                self._live.pop(task_id, None)
            self._deferred.pop(task_id, None)
            self.store.set_task_status(
                task_id, "cancelled", result_summary="Cancelled by the owner."
            )
            return "cancelled"
        # running: flag it (the store row tells the truth even if the signal
        # races) and cancel the executing asyncio task — the cancellation
        # lands at the brain's next await point, between tool calls.
        loop, current = self._loop, self._current
        if current is not None and self._current_id == task_id:
            if loop is not None and loop.is_running():
                loop.call_soon_threadsafe(current.cancel)
            else:
                # Inline mode: cancel() is called from the loop that owns
                # the task (a /stop arriving on the same transport loop).
                current.cancel()
        return "cancelling"

    # -------------------------------------------------------------- misc ---

    def running_task_for(self, session_key):
        for row in self.store.list_tasks(
            session_key=str(session_key), statuses=("running",), limit=1
        ):
            return row
        return None

    def wait_for(self, task_id, timeout=10.0, poll=0.05):
        """Block until a task reaches a terminal state (tests, tooling)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            row = self.store.get_task(task_id)
            if row is not None and row["status"] in ("done", "failed", "cancelled"):
                return row
            time.sleep(poll)
        return self.store.get_task(task_id)
