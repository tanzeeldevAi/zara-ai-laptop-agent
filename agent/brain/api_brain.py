"""API brain: one reasoning loop for every LLM provider, via LiteLLM.

Replaces the original Anthropic-only loop. Claude, GPT, Gemini, DeepSeek and
any OpenAI-compatible endpoint (OpenRouter, Groq, Mistral, xAI, Together,
local Ollama/LM Studio) all run through this single loop; the provider
registry (providers.py) supplies the model string, key and capability flags.

Wire-format note: providers disagree on how images may ride inside tool
results, so tool results are sent as plain text and any screenshots follow in
one user message — the standard vision input every provider accepts. What the
owner experiences is unchanged: the model sees the screen, files and
screenshots are delivered through the channel as before.

A model without vision gets the NO_VISION_NOTICE in its system prompt and
never receives image data — it is told, loudly, instead of failing silently.
"""

import asyncio
import base64
import json
import traceback
from pathlib import Path

import litellm

from ..config import CONFIG
from ..prompts import SYSTEM_PROMPT
from ..safety import ToolExecutor
from ..store import SqliteHistoryStore, get_store
from ..tools import load_all
from .providers import NO_VISION_NOTICE, build_model_string, get_provider, model_supports_vision

SCHEMAS, _HANDLERS = load_all()

# Registry schemas are Anthropic-shaped; LiteLLM wants the OpenAI tool shape.
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": schema["name"],
            "description": schema["description"],
            "parameters": schema["input_schema"],
        },
    }
    for schema in SCHEMAS
]


def _encode_image(path):
    return base64.standard_b64encode(Path(path).read_bytes()).decode("ascii")


def _sanitize_loaded(messages):
    """Make a history restored from the store safe to send to a provider.

    A turn interrupted mid-flight (cancel, crash) can leave a dangling
    assistant tool-call group — an assistant message whose tool results
    never landed — which strict providers reject outright. Drop incomplete
    groups and orphan tool results, and apply _trim's front rule so the
    sequence starts with a plain user message.
    """
    while messages and not (
        messages[0].get("role") == "user" and isinstance(messages[0].get("content"), str)
    ):
        messages.pop(0)

    cleaned = []
    index = 0
    while index < len(messages):
        message = messages[index]
        calls = message.get("tool_calls") if message.get("role") == "assistant" else None
        if calls:
            ids = [call.get("id") for call in calls]
            following = messages[index + 1 : index + 1 + len(ids)]
            complete = len(following) == len(ids) and all(
                part.get("role") == "tool" and part.get("tool_call_id") == call_id
                for part, call_id in zip(following, ids)
            )
            if complete:
                cleaned.append(message)
                cleaned.extend(following)
                index += 1 + len(ids)
            else:
                index += 1
                while index < len(messages) and messages[index].get("role") == "tool":
                    index += 1
            continue
        if message.get("role") == "tool":
            index += 1          # orphan tool result: its call was dropped
            continue
        cleaned.append(message)
        index += 1
    return cleaned


class ApiBrain:
    """Same surface as the original brain.Brain, backed by LiteLLM."""

    def __init__(self, executor=None, history_store=None):
        self.provider = get_provider(CONFIG.api_provider)
        self.model_name = CONFIG.provider_model
        self.model_string = build_model_string(self.provider, self.model_name)
        self.supports_vision = model_supports_vision(self.provider, self.model_name)
        self.executor = executor or ToolExecutor(brain_label=f"api:{self.provider.id}", announce=True)
        # Phase 3: conversation history is backed by SQLite — the in-memory
        # list stays the working copy, the store is what survives a restart.
        self.history_store = history_store or SqliteHistoryStore(get_store())
        self.sessions = {}
        self.busy = set()

    @property
    def model_label(self):
        return self.model_string

    def reset(self, key):
        self.sessions.pop(key, None)
        self.history_store.clear(key)

    def _history(self, key):
        if key not in self.sessions:
            self.sessions[key] = _sanitize_loaded(self.history_store.load(key))
        return self.sessions[key]

    def _persist(self, key, message):
        """Mirror one message into the durable history store.

        The screenshot-carrying user message (a content LIST with base64
        image data) is deliberately not persisted: it would bloat the
        database enormously, and dropping a standalone user message keeps
        the restored sequence valid.
        """
        if isinstance(message.get("content"), list):
            return
        self.history_store.append(key, message)

    def _trim(self, messages):
        limit = max(4, CONFIG.history_turns)
        while len(messages) > limit:
            # never leave a dangling tool message at the front, providers reject it
            messages.pop(0)
            while messages and not (
                messages[0]["role"] == "user" and isinstance(messages[0]["content"], str)
            ):
                messages.pop(0)

    def _system(self):
        if self.supports_vision:
            return SYSTEM_PROMPT
        return NO_VISION_NOTICE + "\n\n" + SYSTEM_PROMPT

    def _call_kwargs(self):
        kwargs = {
            "model": self.model_string,
            "messages": [],
            "tools": TOOLS,
            "max_tokens": 4096,
        }
        key = CONFIG.provider_key()
        if not key and self.provider.id == "custom":
            key = "local-no-key"   # local servers (Ollama/LM Studio) ignore it
        if key:
            kwargs["api_key"] = key
        if self.provider.id == "custom" and CONFIG.custom_api_base:
            kwargs["api_base"] = CONFIG.custom_api_base
        return kwargs

    async def handle(self, key, user_text, chan):
        """Run one instruction to completion, reporting progress through `chan`."""
        if key in self.busy:
            await chan.send_text("I'm still working on the previous instruction. Send `/stop` to cancel it.")
            return
        self.busy.add(key)
        try:
            await self._run(key, user_text, chan)
        except asyncio.CancelledError:
            await chan.send_text("Cancelled.")
            raise
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            await chan.send_text(f"Agent error: `{type(exc).__name__}: {exc}`")
        finally:
            self.busy.discard(key)

    async def _run(self, key, user_text, chan):
        messages = self._history(key)
        user_message = {"role": "user", "content": user_text}
        messages.append(user_message)
        self._persist(key, user_message)
        self._trim(messages)

        for _step in range(CONFIG.max_iterations):
            kwargs = self._call_kwargs()
            kwargs["messages"] = [{"role": "system", "content": self._system()}] + messages
            response = await litellm.acompletion(**kwargs)
            message = response.choices[0].message

            text = message.content if isinstance(message.content, str) else ""
            calls = []
            for tool_call in getattr(message, "tool_calls", None) or []:
                fn = tool_call.function
                calls.append(
                    {
                        "id": tool_call.id,
                        "name": fn.name,
                        "arguments": fn.arguments or "{}",
                    }
                )

            assistant_msg = {"role": "assistant", "content": text}
            if calls:
                assistant_msg["tool_calls"] = [
                    {
                        "id": c["id"],
                        "type": "function",
                        "function": {"name": c["name"], "arguments": c["arguments"]},
                    }
                    for c in calls
                ]
            messages.append(assistant_msg)
            self._persist(key, assistant_msg)

            if text.strip():
                await chan.send_text(text)

            if not calls:
                return

            image_parts = []
            for call in calls:
                if isinstance(call["arguments"], str):
                    try:
                        args = json.loads(call["arguments"] or "{}")
                    except json.JSONDecodeError:
                        args = {}
                else:
                    args = call["arguments"] or {}

                outcome = await self.executor.execute(
                    call["name"], args, chan, show_images_to_user=True
                )
                result_text = ("ERROR: " if outcome.is_error else "") + outcome.text
                tool_message = {
                    "role": "tool", "tool_call_id": call["id"], "content": result_text,
                }
                messages.append(tool_message)
                self._persist(key, tool_message)

                if self.supports_vision:
                    for image_path in outcome.images:
                        try:
                            image_parts.append(
                                {
                                    "type": "image_url",
                                    "image_url": {
                                        "url": f"data:image/jpeg;base64,{_encode_image(image_path)}"
                                    },
                                }
                            )
                        except Exception as exc:  # noqa: BLE001
                            image_parts.append(
                                {"type": "text", "text": f"(could not attach image: {exc})"}
                            )

            if image_parts:
                messages.append(
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "Screenshot(s) from the tool results above:"}
                        ]
                        + image_parts,
                    }
                )

        await chan.send_text(
            f"Stopped after {CONFIG.max_iterations} steps without finishing. "
            "Tell me the next step, or raise MAX_ITERATIONS in .env."
        )


# Backwards-compatible name (the original class in agent/brain.py).
Brain = ApiBrain
