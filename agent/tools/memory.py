"""Persistent memory tools: facts the owner asks the assistant to keep.

Stored in the SQLite store (agent/store.py, facts table) — they survive
restarts, unlike conversation history. Facts are the OWNER's own notes
about their world (names, preferences, project folders); no secrets belong
here — API keys live in the OS keyring, never in this store.
"""

from . import tool
from ..store import get_store

MAX_FACTS_LISTED = 100


@tool(
    "remember_fact",
    "Store a durable fact in long-term memory, so it survives restarts — e.g. "
    "the owner's preferences, important names, project folder locations, "
    "recurring details. Use a short stable key ('wifi_password_hint' is a bad "
    "idea — never store passwords or API keys here; 'office_address', "
    "'brother_name', 'invoice_folder' are good). Overwrites any existing fact "
    "with the same key.",
    {
        "key": {
            "type": "string",
            "description": "Short identifier for the fact, e.g. 'printer_location'.",
        },
        "value": {
            "type": "string",
            "description": "The fact itself, in plain words.",
        },
    },
    required=["key", "value"],
)
def remember_fact(key, value):
    key = str(key or "").strip()
    value = str(value or "").strip()
    if not key or not value:
        return {"text": "remember_fact needs both a key and a value."}
    get_store().set_fact(key, value)
    return {"text": f"Remembered — {key}: {value}"}


@tool(
    "recall_facts",
    "List facts stored in long-term memory. With no query, lists everything; "
    "with a query, only facts whose key or value contains it. Use this when "
    "the owner refers to something they told you before ('my office', 'the "
    "usual folder') before asking them to repeat it.",
    {
        "query": {
            "type": "string",
            "description": "Optional substring to filter facts by.",
        },
    },
)
def recall_facts(query=""):
    facts = get_store().list_facts(str(query).strip() or None)
    if not facts:
        if query:
            return {"text": f"No stored facts match '{query}'."}
        return {"text": "No facts stored yet."}
    lines = [f"{key}: {value}" for key, value in facts[:MAX_FACTS_LISTED]]
    text = "\n".join(lines)
    if len(facts) > MAX_FACTS_LISTED:
        text += f"\n... and {len(facts) - MAX_FACTS_LISTED} more (narrow it with a query)"
    return {"text": text}


@tool(
    "forget_fact",
    "Permanently delete one fact from long-term memory by its exact key. "
    "This cannot be undone.",
    {
        "key": {
            "type": "string",
            "description": "The exact key of the fact to delete.",
        },
    },
    required=["key"],
)
def forget_fact(key):
    key = str(key or "").strip()
    if not key:
        return {"text": "forget_fact needs the exact key of the fact to delete."}
    if get_store().delete_fact(key):
        return {"text": f"Forgotten — the fact '{key}' has been deleted."}
    return {"text": f"No fact is stored under the key '{key}', so nothing was deleted."}
