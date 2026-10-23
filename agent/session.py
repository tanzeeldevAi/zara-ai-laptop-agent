"""Conversation session: what happens to one instruction, transport-neutral.

Before Phase 0 this flow lived inline in the Discord client's on_message.
It now sits behind the Transport seam: a transport gates the sender against
the allowlist, packages the message as an IncomingMessage, and hands it here.

Phase 3: an accepted instruction no longer runs inline (with a flat
"still busy" rejection when a job was already running). It is submitted to
the unified task queue (agent/queue.py) — durable, global FIFO across all
sessions and transports — and the queue's worker delivers the brain's
replies back through this session's channel. When the queue's worker
thread was never started (tests, embedding), the session executes the task
inline on the caller's loop instead, which behaves exactly as before.
Commands (/help, /new, /stop, /queue, /cancel, ...) are never queued.
"""

import asyncio
import contextlib

from . import licensing, voice
from .config import CONFIG
from .queue import LoopBoundChannel, TaskQueue
from .store import get_store
from .transport import session_key_of

HELP = """**{name} - your laptop, from your phone**

Talk to me in plain English - by text OR by voice note (just send a voice message from your chat app).
I understand English and Urdu, and I'll reply by voice when you talk to me by voice.

> what's on my screen right now?
> is the laptop plugged in and how much battery is left?
> open chrome and go to gmail, then screenshot it
> WhatsApp par Ali ko bhejo: main 10 min mein aa raha hoon
> find every invoice PDF in Documents from this year and send me the biggest one
> what's using all my RAM? close the worst offender

**Commands**
`/new` - forget this conversation and start fresh
`/stop` - cancel whatever I'm doing right now
`/queue` - see what's queued up
`/cancel <id>` - drop a queued task (id from /queue)
`/shot` - instant screenshot, no thinking
`/status` - instant system health
`/license` - your license / trial status
`/voice on` / `/voice off` / `/voice auto` - control voice replies
`/help` - this message"""


class AgentSession:
    def __init__(self, brain, brain_label="", store=None, queue=None,
                 license_manager=None):
        self.brain = brain
        self.brain_label = brain_label
        self.store = store or get_store()
        # Phase 4: licensing gate. On a development build (no public key
        # configured) the manager always reports "development" = allowed,
        # so behaviour is exactly as before unless a release build turns
        # enforcement on.
        self.license_manager = license_manager or licensing.get_manager(self.store)
        self.queue = queue or TaskQueue(
            self.store, self._run_task, channel_resolver=self._resolve_channel
        )
        self.voice_mode = {}    # session_key -> per-channel voice override
        self._channels = {}     # session_key -> latest LoopBoundChannel seen

    @property
    def model_label(self):
        return getattr(self.brain, "model_label", "")

    def _resolve_channel(self, session_key):
        """Latest channel seen for a session (queue resolver for tasks that
        arrive without a live channel: scheduled and crash-recovered ones)."""
        return self._channels.get(str(session_key))

    async def handle_message(self, msg):
        chan = msg.channel
        key = session_key_of(chan)
        text = (msg.text or "").strip()

        # Bind the channel to this loop and remember it, so queued and
        # proactive work can deliver back to this chat later.
        bound = LoopBoundChannel(chan, asyncio.get_running_loop())
        self._channels[str(key)] = bound

        # Phase 4: the license gate sits before everything except /license
        # and /help — in activation mode the brain is never invoked and
        # nothing is queued; the owner just gets the activation notice.
        if not await self._license_gate(text, bound):
            return

        if text.startswith("/"):
            if await self._command(text, key, bound):
                return

        came_by_voice = False
        if msg.audio_paths or msg.audio_failed:
            heard, lang = "", ""
            if msg.audio_paths:
                heard, lang = await self._listen(msg.audio_paths[0], bound)
            if heard:
                # tell the model it arrived as speech, so it replies by voice in the same language
                spoken_lang = "Urdu" if str(lang).startswith("ur") else (
                    "Hindi/Urdu" if str(lang).startswith("hi") else "English"
                )
                text = (
                    f"{text}\n{heard}".strip()
                    + f"\n\n[This came in as a VOICE NOTE spoken in {spoken_lang}. "
                    f"Reply using the `speak` tool in the same language, plus a short text line.]"
                )
                came_by_voice = True
                await bound.send_text(f"-# 🎙️ heard: {heard[:300]}")
            elif not text:
                await bound.send_text(
                    "I got your voice note but couldn't make out any words. "
                    "Try speaking a bit longer, or type it."
                )
                return

        if not text and not msg.other_attachments:
            return

        if msg.other_attachments:
            names = ", ".join(msg.other_attachments)
            text = f"{text}\n\n(The user attached: {names} - these are on their phone, not the laptop.)"

        voice.SPOKE_CHANNELS.discard(key)   # fresh turn: no voice sent yet

        task_id, ahead = self.queue.submit(
            key, text, chan=bound, came_by_voice=came_by_voice
        )
        if self.queue.is_running:
            if ahead > 0:
                await bound.send_text(
                    f"⏳ Queued as task #{task_id} — position {ahead + 1} in line. "
                    f"I'll start it as soon as the work ahead of it finishes. "
                    f"`/queue` shows the line; `/cancel {task_id}` drops it."
                )
            return

        # Worker not started (tests / embedding): run it here, as before.
        try:
            await self.queue.run_inline(task_id)
        except asyncio.CancelledError:
            pass

    # ---------------------------------------------------------- licensing --

    def _license_status(self):
        try:
            return self.license_manager.status()
        except Exception:  # noqa: BLE001 - a licensing bug must not brick
            return None          # the agent; status() itself is local+simple

    async def _license_gate(self, text, chan):
        """True when the message may proceed. In activation mode, answers
        /license itself and replies to everything else with the notice."""
        status = self._license_status()
        if status is None or status.allowed:
            return True
        command = text.split()[0].lower() if text.startswith("/") else ""
        if command == "/license":
            await chan.send_text(licensing.license_text(status))
            return False
        if command in ("/help", "/start"):
            return True     # help stays readable; it performs no work
        await chan.send_text(licensing.activation_notice(status))
        return False

    async def _run_task(self, row, chan, live):
        """Execute one queued task: the brain turn + the spoken reply.

        Runs on the queue worker's loop in production; `chan` is a
        LoopBoundChannel, so every send is marshalled back to the
        transport's own loop.
        """
        key = row["session_key"]
        came_by_voice = live.came_by_voice if live is not None else False

        # A task queued before the license lapsed (or a scheduled one) must
        # not reach the brain in activation mode either.
        status = self._license_status()
        if status is not None and not status.allowed:
            await chan.send_text(licensing.activation_notice(status))
            return

        # The original showed Discord's "typing…" for the whole job; channels
        # that support it expose typing(), the rest skip it.
        typing_cm = chan.typing() if hasattr(chan, "typing") else contextlib.nullcontext()
        async with typing_cm:
            await self.brain.handle(key, row["prompt"], chan)

        await self._maybe_speak(key, came_by_voice, chan)

    async def _listen(self, path, chan):
        """Transcribe an already-downloaded voice note. Returns (text, language)."""
        try:
            if not voice.available():
                await chan.send_text(
                    "Voice recognition isn't installed. Run `install.bat` again to add it, "
                    "or just type your command."
                )
                return "", ""
            return await asyncio.to_thread(voice.transcribe, path)
        finally:
            try:
                path.unlink()
            except OSError:
                pass

    async def _maybe_speak(self, key, came_by_voice, chan):
        # The `speak` tool already sent audio this turn -> never send a second clip.
        # Checked by channel ID because a brain may hand tools a stale channel object.
        spoke_via_tool = key in voice.SPOKE_CHANNELS
        voice.SPOKE_CHANNELS.discard(key)
        if spoke_via_tool or getattr(chan, "spoke", False):
            return
        mode = self.voice_mode.get(key, CONFIG.voice_replies)
        if mode == "never" or mode == "off":
            return
        if mode in ("auto", "") and not came_by_voice:
            return
        # mode == 'always'/'on', or 'auto' with a voice input
        spoken = chan.last_spoken.strip()
        if not spoken:
            return
        clip = await voice.synthesize(spoken)
        if clip:
            await chan.send_file(clip, None)

    async def _command(self, text, key, chan):
        command = text.split()[0].lower()

        if command in ("/help", "/start"):
            await chan.send_text(HELP.format(name=CONFIG.assistant_name))
            return True

        if command == "/license":
            status = self._license_status()
            if status is None:
                await chan.send_text("License status is unavailable right now.")
            else:
                await chan.send_text(licensing.license_text(status))
            return True

        if command == "/voice":
            arg = (text.split() + [""])[1].lower()
            if arg in ("on", "always"):
                self.voice_mode[key] = "always"
                await chan.send_text("Voice replies: ON - I'll speak every reply.")
            elif arg in ("off", "never"):
                self.voice_mode[key] = "never"
                await chan.send_text("Voice replies: OFF - text only.")
            elif arg in ("auto", ""):
                self.voice_mode[key] = "auto"
                await chan.send_text("Voice replies: AUTO - I'll speak when you talk to me by voice.")
            else:
                await chan.send_text("Use `/voice on`, `/voice off`, or `/voice auto`.")
            return True

        if command == "/new":
            self.brain.reset(key)
            await chan.send_text("Fresh start - I've forgotten the previous conversation.")
            return True

        if command == "/stop":
            running = self.queue.running_task_for(key)
            if running is not None:
                self.queue.cancel(running["id"])
                await chan.send_text("Stopping.")
            else:
                await chan.send_text("Nothing running.")
            return True

        if command == "/queue":
            rows = self.store.list_tasks(
                session_key=str(key), statuses=("queued", "running"), limit=20
            )
            if not rows:
                await chan.send_text("Nothing queued — I'm all caught up.")
                return True
            lines = []
            for row in rows:
                prompt = " ".join(str(row["prompt"]).split())[:70]
                if row["status"] == "running":
                    lines.append(f"#{row['id']} ▶️ running — {prompt}")
                else:
                    lines.append(
                        f"#{row['id']} ⏳ position {self.queue.position(row['id'])} — {prompt}"
                    )
            await chan.send_text("In line:\n" + "\n".join(lines))
            return True

        if command == "/cancel":
            parts = text.split()
            task_id = None
            if len(parts) > 1:
                try:
                    task_id = int(parts[1].lstrip("#"))
                except ValueError:
                    task_id = None
            if task_id is None:
                await chan.send_text("Use `/cancel <id>` — the id is shown by `/queue`.")
                return True
            row = self.store.get_task(task_id)
            if row is None or str(row["session_key"]) != str(key):
                await chan.send_text(f"Task #{task_id} isn't in this chat.")
                return True
            outcome = self.queue.cancel(task_id)
            if outcome == "cancelled":
                await chan.send_text(f"Task #{task_id} cancelled — it won't run.")
            elif outcome == "cancelling":
                await chan.send_text(f"Cancelling task #{task_id}…")
            else:
                await chan.send_text(f"Task #{task_id} already finished.")
            return True

        if command == "/shot":
            from .tools.screen import screenshot

            result = await asyncio.to_thread(screenshot)
            for path in result.get("images", []):
                await chan.send_file(path)
            return True

        if command == "/status":
            from .tools.system import system_status

            result = await asyncio.to_thread(system_status)
            await chan.send_text(f"```\n{result['text']}\n```")
            return True

        return False
