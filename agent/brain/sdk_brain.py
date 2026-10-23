"""Subscription brain: runs the assistant on your Claude Code login instead
of a paid API key.

Uses the official Claude Agent SDK, which authenticates through the Claude Code
CLI you're already signed into. Same models as the API (Opus etc.) - no quality
loss - but billed to your Claude subscription rather than per-token API credits.

Our laptop tools are exposed to the SDK as an in-process MCP server.
Phase 0 change: the MCP wrappers no longer call handlers directly — every call
goes through the shared ToolExecutor, so the safety pipeline (classify ->
confirmation -> audit) applies to this brain exactly as it does to the API
brain. Previously this path never consulted security.classify at all, so
CONFIRM_RISKY=true silently did nothing here. Screenshots are handed to the
model as vision AND pushed to the channel (when the tool asks), via a
contextvar that carries the active channel into each tool call.
"""

import asyncio
import base64
import contextvars
import traceback
from pathlib import Path

import claude_agent_sdk as sdk

from ..config import CONFIG
from ..prompts import SYSTEM_PROMPT
from ..safety import ToolExecutor
from ..tools import load_all

SCHEMAS, _HANDLERS = load_all()

# Where tool output (screenshots, files) gets sent.
#
# A contextvar alone is NOT enough: the SDK captures a context snapshot when the session
# connects and runs tool handlers under it. Because we pre-warm the session before any
# message arrives, that snapshot has no channel - so tools saw None and silently dropped
# every screenshot. _ACTIVE is the authoritative current channel, updated per turn.
_CHAN = contextvars.ContextVar("lappilot_channel", default=None)
_ACTIVE = None


def _current_channel():
    return _ACTIVE if _ACTIVE is not None else _CHAN.get()


def _encode(path):
    return base64.standard_b64encode(Path(path).read_bytes()).decode("ascii")


def _build_tool(schema, executor):
    name = schema["name"]

    @sdk.tool(name, schema["description"], schema["input_schema"])
    async def _run(args, _executor=executor, _name=name):
        chan = _current_channel()
        outcome = await _executor.execute(_name, args or {}, chan, show_images_to_user=False)

        content = [{"type": "text", "text": outcome.text}]
        # Images always go to the model so it can see the screen. The executor
        # has already forwarded to the channel only what the tool asked to show.
        for image_path in outcome.images:
            try:
                content.append({"type": "image", "data": _encode(image_path), "mimeType": "image/jpeg"})
            except Exception as exc:  # noqa: BLE001
                content.append({"type": "text", "text": f"(image attach failed: {exc})"})

        result = {"content": content}
        if outcome.is_error:
            result["isError"] = True
        return result

    return _run


def _make_server(executor):
    tools = [_build_tool(schema, executor) for schema in SCHEMAS]
    server = sdk.create_sdk_mcp_server("laptop", "1.0.0", tools)
    allowed = [f"mcp__laptop__{schema['name']}" for schema in SCHEMAS]
    return server, allowed


class SdkBrain:
    """Same surface as the API brain, backed by the Claude subscription."""

    def __init__(self, executor=None):
        self.executor = executor or ToolExecutor(brain_label="subscription")
        self._server, self._allowed = _make_server(self.executor)
        self.clients = {}   # channel id -> connected ClaudeSDKClient (keeps conversation memory)
        self.busy = set()
        self._spare = None  # pre-connected client, so the first message doesn't pay startup cost

    @property
    def model_label(self):
        return CONFIG.subscription_model or "default"

    def _options(self):
        kwargs = dict(
            system_prompt=SYSTEM_PROMPT,
            mcp_servers={"laptop": self._server},
            allowed_tools=self._allowed,
            permission_mode="bypassPermissions",
            max_turns=CONFIG.max_iterations,
            # keep the assistant on our tools, not Claude Code's own file/bash tools
            setting_sources=[],
        )
        if CONFIG.subscription_model:
            kwargs["model"] = CONFIG.subscription_model
        return sdk.ClaudeAgentOptions(**kwargs)

    async def prewarm(self):
        """Connect a session in advance. First reply drops from ~17s to ~3s."""
        if self._spare is not None or self.clients:
            return
        try:
            client = sdk.ClaudeSDKClient(options=self._options())
            await client.connect()
            self._spare = client
            print("[brain] Claude session pre-warmed")
        except Exception as exc:  # noqa: BLE001
            print(f"[brain] prewarm skipped: {exc}")

    def reset(self, key):
        client = self.clients.pop(key, None)
        if client is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no loop (interpreter shutting down) - nothing to schedule
        loop.create_task(self._disconnect(client))

    async def aclose(self):
        """Disconnect every live session - call on shutdown."""
        clients = list(self.clients.values())
        self.clients.clear()
        for client in clients:
            await self._disconnect(client)

    @staticmethod
    async def _disconnect(client):
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001
            pass

    async def _client(self, key):
        client = self.clients.get(key)
        if client is None:
            if self._spare is not None:      # use the pre-warmed session
                client, self._spare = self._spare, None
            else:
                client = sdk.ClaudeSDKClient(options=self._options())
                await client.connect()
            self.clients[key] = client
        return client

    async def handle(self, key, user_text, chan):
        if key in self.busy:
            await chan.send_text("I'm still working on the previous instruction. Send `/stop` to cancel it.")
            return
        global _ACTIVE

        self.busy.add(key)
        token = _CHAN.set(chan)
        _ACTIVE = chan          # authoritative: survives the SDK's context snapshot
        try:
            client = await self._client(key)
            prompt = user_text
            rounds_left = max(0, CONFIG.auto_continue)

            while True:
                await client.query(prompt)

                spoke = False
                hit_limit = False
                async for message in client.receive_response():
                    if isinstance(message, sdk.AssistantMessage):
                        for block in message.content:
                            if isinstance(block, sdk.TextBlock) and block.text.strip():
                                spoke = True
                                await chan.send_text(block.text)
                    elif isinstance(message, sdk.ResultMessage):
                        subtype = str(getattr(message, "subtype", "") or "")
                        # The SDK stops silently once max_turns is reached. Long jobs used to
                        # die here with no message at all, hours into the work.
                        hit_limit = "max_turns" in subtype
                        if getattr(message, "is_error", False) and not hit_limit and not spoke:
                            await chan.send_text(
                                "I hit an error and couldn't finish that one. Try `/new` and rephrase."
                            )
                        break

                if not hit_limit:
                    return

                if rounds_left <= 0:
                    await chan.send_text(
                        f"I've done {CONFIG.max_iterations} steps and paused so this doesn't run "
                        f"forever. Say **continue** and I'll pick up where I left off."
                    )
                    return

                rounds_left -= 1
                await chan.send_text("-# still working, continuing…")
                prompt = ("Continue exactly where you stopped. Do not start over and do not "
                          "repeat work you have already finished.")
        except asyncio.CancelledError:
            self.reset(key)   # a half-consumed stream would corrupt the session
            await chan.send_text("Cancelled.")
            raise
        except sdk.CLINotFoundError:
            await chan.send_text(
                "The Claude Code CLI isn't available, so subscription mode can't run. Install it with "
                "`npm i -g @anthropic-ai/claude-code` and sign in, or add an ANTHROPIC_API_KEY to .env."
            )
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self.reset(key)
            await chan.send_text(f"Agent error: `{type(exc).__name__}: {exc}`")
        finally:
            self.busy.discard(key)
            _CHAN.reset(token)
            if _ACTIVE is chan:      # don't clobber a turn that started meanwhile
                _ACTIVE = None
