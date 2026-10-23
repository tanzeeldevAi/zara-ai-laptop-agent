"""Scheduler: fire stored prompts into the task queue at the right time.

All times are the LAPTOP's local time (naive local datetimes, stored as epoch
seconds) — the owner thinks in wall-clock time, and the laptop lives in one
place, so no timezone conversion is applied anywhere.

Schedule kinds (spec is a small JSON dict in the schedules table):
    daily     {time: "HH:MM"}                    every day at HH:MM
    weekly    {time: "HH:MM", weekday: 0..6}     weekly (Monday = 0)
    interval  {minutes: N}                       every N minutes
    once      {at: "YYYY-MM-DD HH:MM"}           one shot, then disabled

Catch-up rule: if the agent was off when a schedule came due, it fires ONCE
when the agent is back — never a burst of missed repetitions. Recurring
schedules recompute their next run from "now", which collapses all missed
occurrences into that single catch-up firing.
"""

import threading
import time
from datetime import datetime, timedelta

POLL_SECONDS = 20

WEEKDAY_NAMES = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def parse_hhmm(text):
    """'HH:MM' -> (hour, minute), or None when malformed/out of range."""
    try:
        hh, mm = str(text).strip().split(":")
        hour, minute = int(hh), int(mm)
    except (ValueError, AttributeError):
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


def parse_weekday(value):
    """Accept 0..6, '0'..'6', or an English weekday name/prefix -> 0..6 or None."""
    if isinstance(value, int):
        return value if 0 <= value <= 6 else None
    text = str(value or "").strip().lower()
    if text.isdigit():
        number = int(text)
        return number if 0 <= number <= 6 else None
    for index, name in enumerate(WEEKDAY_NAMES):
        if name.startswith(text) and len(text) >= 3:
            return index
    return None


def compute_next_run(kind, spec, from_dt=None):
    """Next local datetime this schedule fires, strictly after `from_dt`.

    For 'once', returns the target itself (which may be in the past — the
    caller validates that at creation time). Raises ValueError on bad input.
    """
    now = from_dt or datetime.now()
    spec = spec or {}
    if kind == "interval":
        minutes = int(spec.get("minutes") or 0)
        if minutes <= 0:
            raise ValueError("interval schedules need minutes > 0")
        return now + timedelta(minutes=minutes)
    if kind == "once":
        at = spec.get("at") or ""
        try:
            return datetime.fromisoformat(str(at).strip())
        except ValueError:
            raise ValueError(f"could not parse 'at' datetime: {at!r} (use YYYY-MM-DD HH:MM)")
    if kind in ("daily", "weekly"):
        parsed = parse_hhmm(spec.get("time") or "")
        if parsed is None:
            raise ValueError(f"bad time {spec.get('time')!r} (use HH:MM, 24-hour)")
        hour, minute = parsed
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if kind == "daily":
            if candidate <= now:
                candidate += timedelta(days=1)
            return candidate
        weekday = parse_weekday(spec.get("weekday"))
        if weekday is None:
            raise ValueError(f"bad weekday {spec.get('weekday')!r} (0=Monday..6=Sunday or a name)")
        while candidate.weekday() != weekday or candidate <= now:
            candidate += timedelta(days=1)
        return candidate
    raise ValueError(f"unknown schedule kind: {kind!r}")


def describe_schedule(row):
    """One human-readable line for a schedule row."""
    spec = row.get("spec") or {}
    kind = row.get("kind")
    if kind == "daily":
        when = f"daily at {spec.get('time')}"
    elif kind == "weekly":
        weekday = parse_weekday(spec.get("weekday"))
        name = WEEKDAY_NAMES[weekday].capitalize() if weekday is not None else "?"
        when = f"weekly on {name} at {spec.get('time')}"
    elif kind == "interval":
        when = f"every {spec.get('minutes')} min"
    elif kind == "once":
        when = f"once at {spec.get('at')}"
    else:
        when = str(kind)
    nxt = row.get("next_run")
    nxt_text = (
        datetime.fromtimestamp(nxt).strftime("%Y-%m-%d %H:%M") if nxt else "?"
    )
    state = "" if row.get("enabled", 1) else " (disabled)"
    return f"#{row.get('id')} [{when}] next: {nxt_text}{state} — {row.get('prompt', '')}"


class Scheduler:
    """Background thread: due schedules become queued tasks.

    The destination chat is the schedule's stored session_key; the task
    queue resolves a live channel for it at run time (see agent/queue.py).
    """

    def __init__(self, store, queue, poll_seconds=POLL_SECONDS):
        self.store = store
        self.queue = queue
        self.poll_seconds = poll_seconds
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop, name="scheduler", daemon=True
        )
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.run_due()
            except Exception as exc:  # noqa: BLE001 - the scheduler must not die
                print(f"[scheduler] error: {type(exc).__name__}: {exc}")
            self._stop.wait(self.poll_seconds)

    def run_due(self, now=None):
        """Fire every due schedule once. Returns how many tasks were queued."""
        now_dt = now or datetime.now()
        due = self.store.due_schedules(now_dt.timestamp())
        fired = 0
        for row in due:
            self.queue.submit(row["session_key"], row["prompt"])
            fired += 1
            kind = row["kind"]
            if kind == "once":
                self.store.update_schedule(
                    row["id"], enabled=0, last_run=now_dt.timestamp()
                )
            else:
                try:
                    nxt = compute_next_run(kind, row.get("spec") or {}, from_dt=now_dt)
                except ValueError:
                    # A schedule we can no longer interpret: disable it
                    # rather than refiring every poll.
                    self.store.update_schedule(
                        row["id"], enabled=0, last_run=now_dt.timestamp()
                    )
                    continue
                self.store.update_schedule(
                    row["id"], next_run=nxt.timestamp(), last_run=now_dt.timestamp()
                )
        return fired
