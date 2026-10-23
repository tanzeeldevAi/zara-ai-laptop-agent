"""The shared safety pipeline: every tool call, from every brain, passes here.

    allowlist (checked by the transport before a message reaches a brain)
      -> security.classify()   (is this action risky?)
      -> confirmation          (only when CONFIG.confirm_risky is on)
      -> audit log hook        (one JSON line per tool call, always)

Phase 0 centralises what used to live inside the API brain only: the
subscription (SDK) brain never called security.classify, so CONFIRM_RISKY=true
silently did nothing there. Now both brains execute tools exclusively through
ToolExecutor, so the policy cannot be bypassed by choosing a different brain.

Behaviour preserved from the original:
  - CONFIRM_RISKY off  -> risky actions run immediately, no prompt (as before).
  - CONFIRM_RISKY on   -> classify() decides; the owner approves/denies through
                          the transport channel; a denial returns the exact
                          "DENIED: ..." tool result the original produced.
  - Tool progress notes: the original brains disagreed (API brain narrated
    every call; SDK brain only under SHOW_TOOL_CALLS). The executor takes a
    per-brain `announce` option so each brain keeps its own original default.
"""

import asyncio
import json
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

from .config import CONFIG
from .context import reset_current_session_key, set_current_session_key
from .security import classify, describe
from .tools import load_all
from .transport import session_key_of as _session_key_of

SCHEMAS, HANDLERS = load_all()

DENIED_TEXT = (
    "DENIED: the owner refused this action. Do not retry it or attempt "
    "an equivalent action another way. Ask them what to do instead."
)


def truncate(text, limit):
    text = text or ""
    return text if len(text) <= limit else text[:limit] + f"\n... [truncated, {len(text)} chars total]"


def preview_args(args, limit=140):
    if not args:
        return ""
    bits = []
    for key, value in args.items():
        text = str(value).replace("\n", " ")
        if len(text) > 60:
            text = text[:60] + "..."
        bits.append(f"{key}={text}")
    joined = ", ".join(bits)
    return joined if len(joined) <= limit else joined[:limit] + "..."


@dataclass
class ToolOutcome:
    """Brain-neutral result of one tool execution."""

    text: str
    is_error: bool = False
    images: list = field(default_factory=list)   # paths shown to the model where it supports vision
    files: list = field(default_factory=list)    # paths delivered to the user
    captions: list = field(default_factory=list)


class AuditLog:
    """Append-only JSON-lines log of tool calls (the audit-log hook).

    Phase 0 keeps this deliberately small — Phase 3 grows it into the full
    SQLite audit store. Writing must never break a tool call.
    """

    def __init__(self, path=None):
        self.path = Path(path) if path else CONFIG.runtime_dir / "audit.jsonl"

    def record(self, *, brain, tool, args_preview, decision, confirmed=None):
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "brain": brain,
            "tool": tool,
            "args": args_preview,
            "decision": decision,  # allowed | denied | error | unknown-tool
        }
        if confirmed is not None:
            entry["confirmed"] = confirmed
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001 - auditing must never break execution
            pass


class SafetyPipeline:
    """classify -> confirm -> audit, for one prospective tool call."""

    def __init__(self, audit=None):
        self.audit = audit or AuditLog()

    async def check(self, *, brain, name, args, chan):
        """Return None if the call may proceed, else a denial ToolOutcome."""
        needs_confirm, reason = classify(name, args)
        if needs_confirm and CONFIG.confirm_risky:
            if chan is None:
                # Nobody to ask: the safe answer is no.
                self.audit.record(
                    brain=brain, tool=name, args_preview=preview_args(args),
                    decision="denied", confirmed=True,
                )
                return ToolOutcome(DENIED_TEXT, is_error=True)
            approved = await chan.confirm(name, reason, describe(name, args))
            self.audit.record(
                brain=brain, tool=name, args_preview=preview_args(args),
                decision="allowed" if approved else "denied", confirmed=True,
            )
            if not approved:
                return ToolOutcome(DENIED_TEXT, is_error=True)
            return None
        self.audit.record(
            brain=brain, tool=name, args_preview=preview_args(args),
            decision="allowed", confirmed=False if needs_confirm else None,
        )
        return None


class ToolExecutor:
    """The single door through which brains run tools.

    Wraps the pipeline around the plain sync handlers from agent.tools,
    delivers images/files through the channel, and returns a brain-neutral
    ToolOutcome. Both brains construct exactly one executor (or receive one).
    """

    def __init__(self, pipeline=None, *, brain_label="unknown", handlers=None, announce=None):
        self.pipeline = pipeline or SafetyPipeline()
        self.brain_label = brain_label
        self.handlers = handlers if handlers is not None else HANDLERS
        # announce: True = note every call, False = never, None = follow CONFIG.show_tool_calls.
        self.announce = announce

    async def execute(self, name, args, chan, *, show_images_to_user=None):
        """Run one tool. `show_images_to_user` overrides the tool's own
        `show` flag (API brain semantics: always forward; SDK brain: only
        when the tool asks). None = use the tool result's flag."""
        args = args or {}

        denial = await self.pipeline.check(brain=self.brain_label, name=name, args=args, chan=chan)
        if denial is not None:
            return denial

        announce = CONFIG.show_tool_calls if self.announce is None else self.announce
        if announce and chan is not None:
            await chan.note(f"`{name}` {preview_args(args)}")

        handler = self.handlers.get(name)
        if handler is None:
            self.pipeline.audit.record(
                brain=self.brain_label, tool=name, args_preview=preview_args(args),
                decision="unknown-tool",
            )
            return ToolOutcome(f"Unknown tool '{name}'.", is_error=True)

        # Tools that need to know which chat they serve (scheduler tools)
        # read the session key from a contextvar; to_thread copies the
        # context into the handler thread, so set it just before the call.
        token = None
        if chan is not None:
            try:
                token = set_current_session_key(_session_key_of(chan))
            except Exception:  # noqa: BLE001 - context is best-effort
                token = None
        try:
            result = await asyncio.to_thread(handler, **args)
        except TypeError as exc:
            return ToolOutcome(f"Bad arguments for {name}: {exc}", is_error=True)
        except Exception as exc:  # noqa: BLE001 - report, never crash the turn
            traceback.print_exc()
            return ToolOutcome(f"{name} raised {type(exc).__name__}: {exc}", is_error=True)
        finally:
            if token is not None:
                reset_current_session_key(token)

        images = list(result.get("images") or [])
        files = list(result.get("files") or [])
        captions = list(result.get("captions") or [])
        show = bool(result.get("show")) if show_images_to_user is None else show_images_to_user

        if chan is not None:
            if show:
                for index, image_path in enumerate(images):
                    await chan.send_file(image_path, captions[index] if index < len(captions) else None)
            for index, file_path in enumerate(files):
                await chan.send_file(file_path, captions[index] if index < len(captions) else None)
            if name == "speak":
                # Preserve the original duplicate-voice guard exactly — keyed
                # by the same session key the session layer checks.
                chan.spoke = True
                try:
                    from .transport import session_key_of
                    from .voice import SPOKE_CHANNELS

                    SPOKE_CHANNELS.add(session_key_of(chan))
                except Exception:  # noqa: BLE001
                    pass

        return ToolOutcome(
            text=truncate(result.get("text") or "(no output)", CONFIG.max_tool_output),
            images=images,
            files=files,
            captions=captions,
        )
