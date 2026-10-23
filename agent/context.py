"""Per-tool-call context shared across layers.

A contextvar carries the session key of the conversation whose turn is
currently executing a tool. Tool handlers are plain sync functions with no
channel parameter (that is the registry contract), so tools that need to
know WHICH chat they are serving — the scheduler tools, which must send
their future runs back to the originating chat — read it from here.

Set by agent.safety.ToolExecutor around every handler call (asyncio.to_thread
copies the context into the handler thread, so the value survives).
"""

import contextvars

_current_session_key = contextvars.ContextVar(
    "codehubb_current_session_key", default=None
)


def current_session_key():
    """Session key of the turn executing right now, or None outside a turn."""
    return _current_session_key.get()


def set_current_session_key(key):
    return _current_session_key.set(key)


def reset_current_session_key(token):
    _current_session_key.reset(token)
