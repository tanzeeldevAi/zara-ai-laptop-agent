"""Scheduler tools: ask the assistant to do something later, on a rhythm.

A schedule stores a prompt + a destination (the chat it was created from).
When it comes due, the scheduler thread drops the prompt into the unified
task queue, and the reply comes back to this same chat. All times are the
laptop's LOCAL time. See agent/scheduler.py for kinds and catch-up rules.
"""

from datetime import datetime

from . import tool
from .. import scheduler as scheduler_mod
from ..context import current_session_key
from ..store import get_store


@tool(
    "schedule_task",
    "Schedule a prompt to be run later and reported back to THIS chat. "
    "kind='daily' needs time='HH:MM'; kind='weekly' needs time + weekday "
    "(0=Monday..6=Sunday or a weekday name); kind='interval' needs minutes; "
    "kind='once' needs at='YYYY-MM-DD HH:MM'. Times are the laptop's local "
    "time. Examples: a daily 08:00 morning briefing, a weekly Friday report, "
    "a reminder in 30 minutes (interval is recurring — use 'once' with an "
    "exact time for a single reminder).",
    {
        "prompt": {
            "type": "string",
            "description": "What to do when the time comes, phrased as an instruction to yourself.",
        },
        "kind": {
            "type": "string",
            "enum": ["daily", "weekly", "interval", "once"],
            "description": "How often it repeats.",
        },
        "time": {
            "type": "string",
            "description": "HH:MM (24-hour) — for daily and weekly.",
        },
        "weekday": {
            "type": "string",
            "description": "For weekly: 0=Monday..6=Sunday, or a weekday name like 'Friday'.",
        },
        "minutes": {
            "type": "integer",
            "description": "For interval: repeat every N minutes.",
        },
        "at": {
            "type": "string",
            "description": "For once: 'YYYY-MM-DD HH:MM' local time.",
        },
    },
    required=["prompt", "kind"],
)
def schedule_task(prompt, kind, time=None, weekday=None, minutes=None, at=None):
    session_key = current_session_key()
    if session_key is None:
        return {
            "text": "I couldn't tell which chat this schedule belongs to, "
            "so I didn't create it. Try again from your normal chat."
        }
    kind = str(kind or "").strip().lower()
    spec = {}
    if kind in ("daily", "weekly"):
        if not time:
            return {"text": f"A {kind} schedule needs a time (HH:MM)."}
        spec["time"] = str(time).strip()
    if kind == "weekly":
        parsed_weekday = scheduler_mod.parse_weekday(weekday)
        if parsed_weekday is None:
            return {"text": "A weekly schedule needs a weekday (0=Monday..6=Sunday or a name like 'Friday')."}
        spec["weekday"] = parsed_weekday
    if kind == "interval":
        try:
            spec["minutes"] = int(minutes)
        except (TypeError, ValueError):
            return {"text": "An interval schedule needs minutes (a whole number > 0)."}
    if kind == "once":
        if not at:
            return {"text": "A one-time schedule needs 'at' (YYYY-MM-DD HH:MM)."}
        spec["at"] = str(at).strip()

    try:
        next_run = scheduler_mod.compute_next_run(kind, spec)
    except ValueError as exc:
        return {"text": f"Couldn't create that schedule: {exc}"}
    if kind == "once" and next_run <= datetime.now():
        return {"text": "That time is in the past — give me a future time for a one-time schedule."}

    schedule_id = get_store().create_schedule(
        session_key, str(prompt), kind, spec, next_run.timestamp()
    )
    when = next_run.strftime("%Y-%m-%d %H:%M")
    return {
        "text": f"Scheduled as #{schedule_id} ({kind}). First run: {when} (laptop local time). "
        "The result will be sent back to this chat."
    }


@tool(
    "list_schedules",
    "List the active schedules created from this chat — recurring and "
    "one-time prompts, with their next run times and ids.",
    {},
)
def list_schedules():
    session_key = current_session_key()
    rows = get_store().list_schedules(session_key=session_key)
    if not rows:
        return {"text": "No schedules set for this chat."}
    lines = [scheduler_mod.describe_schedule(row) for row in rows]
    return {"text": "Schedules for this chat:\n" + "\n".join(lines)}


@tool(
    "cancel_schedule",
    "Cancel a schedule by its id (see list_schedules). The schedule stops "
    "firing; past runs are unaffected.",
    {
        "id": {
            "type": "integer",
            "description": "The schedule id from list_schedules.",
        },
    },
    required=["id"],
)
def cancel_schedule(id):
    store = get_store()
    row = store.get_schedule(int(id))
    if row is None:
        return {"text": f"No schedule with id {id} exists."}
    session_key = current_session_key()
    if session_key is not None and str(row["session_key"]) != str(session_key):
        return {"text": f"Schedule #{id} belongs to a different chat, so I left it alone."}
    store.delete_schedule(int(id))
    return {"text": f"Schedule #{id} cancelled — it won't fire again."}
