"""WhatsApp transport (Route A): a Baileys sidecar, behind the Transport seam.

WhatsApp has no Discord-style bot API for personal accounts, so a small
Node.js sidecar (sidecar/index.js, @whiskeysockets/baileys) holds the actual
WhatsApp connection — paired once by QR scan, like WhatsApp Web — and talks
to this module over a JSON bridge on 127.0.0.1. The sidecar runs the bridge
as a WebSocket *server*; Python connects as the client, because Python owns
the sidecar's lifecycle (it spawns and supervises the process) and because
the sidecar then keeps the WhatsApp session alive independently of bridge
reconnects. See sidecar/README.md for the full protocol.

Rules that live here (never in the sidecar):
  - the allowlist gate (WHATSAPP_ALLOWED_NUMBERS) — the sidecar forwards
    every 1:1 message and this module drops non-owners instantly;
  - group messages are ignored in v1;
  - confirmations have no native buttons on this route, so they are a
    YES/NO reply: while one is pending for a chat, the owner's YES/NO
    resolves it and any other text is held back with a reminder — the same
    capture semantics as the Discord buttons.
"""

import asyncio
import contextlib
import json
import os
import shutil
import sys
import time
from pathlib import Path

from ..config import CONFIG, ROOT
from . import IncomingMessage

CONFIRM_TIMEOUT = 180             # seconds, same as the Discord/Telegram buttons
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp")
VOICE_EXTS = (".ogg", ".oga", ".opus")
SIDECAR_DIR = ROOT / "sidecar"
LOGGED_OUT_EXIT = 2               # sidecar exit code: pairing is gone, do not restart

YES_WORDS = {"yes", "y", "ہاں", "جی", "جی ہاں"}
NO_WORDS = {"no", "n", "نہیں", "نہ"}


def _digits(text):
    return "".join(ch for ch in str(text or "") if ch.isdigit())


class WhatsAppChannel:
    """What the brains are allowed to do to a WhatsApp chat."""

    def __init__(self, transport, chat_jid, user_number=""):
        self.transport = transport
        self.chat_jid = chat_jid
        self.user_id = int(user_number) if str(user_number).isdigit() else user_number
        self.last_spoken = ""
        self.spoke = False

    @property
    def channel_id(self):
        return self.chat_jid

    @property
    def session_key(self):
        return f"whatsapp:{self.chat_jid}"

    async def send_text(self, text):
        text = str(text)
        if text.strip():
            self.last_spoken = text
        await self.transport.bridge_send({"type": "send_text", "chatJid": self.chat_jid, "text": text})

    async def note(self, text):
        await self.send_text(f"⚙️ {str(text)[:400]}")

    async def send_file(self, path, caption=None):
        target = Path(path)
        if not target.exists():
            return
        suffix = target.suffix.lower()
        if suffix in IMAGE_EXTS:
            await self.transport.bridge_send(
                {"type": "send_image", "chatJid": self.chat_jid, "path": str(target), "caption": caption}
            )
        elif suffix in VOICE_EXTS:
            await self.send_voice(target, caption)
        else:
            await self.transport.bridge_send(
                {"type": "send_file", "chatJid": self.chat_jid, "path": str(target),
                 "kind": "document", "caption": caption}
            )

    async def send_voice(self, path, caption=None):
        target = Path(path)
        if not target.exists():
            return
        await self.transport.bridge_send(
            {"type": "send_voice", "chatJid": self.chat_jid, "path": str(target)}
        )

    async def confirm(self, tool_name, reason, detail):
        return await self.transport.request_confirmation(self, tool_name, reason, detail)


class WhatsAppTransport:
    """Spawns and supervises the sidecar; owns the bridge and the gate."""

    name = "whatsapp"

    def __init__(self, session):
        self.session = session
        self.own_jid = ""
        self._ws = None
        self._pending = {}        # chat_jid -> asyncio.Future (YES/NO confirmation)
        self._proc = None
        self._stopped = False

    # ------------------------------------------------------------------ bridge

    async def bridge_send(self, payload):
        """Send one JSON message to the sidecar. Never raises into a tool call."""
        ws = self._ws
        if ws is None:
            print(f"[whatsapp] bridge not connected; dropping {payload.get('type')}", file=sys.stderr)
            return False
        try:
            await ws.send(json.dumps(payload))
            return True
        except Exception as exc:  # noqa: BLE001
            print(f"[whatsapp] bridge send failed: {exc}", file=sys.stderr)
            return False

    # ------------------------------------------------------------ confirmations

    async def request_confirmation(self, chan, tool_name, reason, detail):
        chat = chan.chat_jid
        existing = self._pending.get(chat)
        if existing is not None and not existing.done():
            return False   # one question at a time, like the button views
        future = asyncio.get_running_loop().create_future()
        self._pending[chat] = future
        await chan.send_text(
            f"⚠️ Confirm before I run this\n\n{tool_name} — {reason}\n\n{detail[:900]}\n\n"
            "Reply YES to run it, NO to cancel. (Auto-cancels in 3 minutes.)"
        )
        try:
            approved = await asyncio.wait_for(future, CONFIRM_TIMEOUT)
        except asyncio.TimeoutError:
            approved = False
        finally:
            self._pending.pop(chat, None)
        await chan.send_text("✅ Approved" if approved else "❌ Cancelled")
        return bool(approved)

    # ------------------------------------------------------------- intake gate

    async def handle_bridge_message(self, data):
        """One parsed JSON message from the sidecar."""
        kind = data.get("type")
        if kind == "ready":
            self.own_jid = data.get("ownJid", "")
            print(f"\n  {CONFIG.product_name} — WhatsApp transport online as {self.own_jid}")
            return
        if kind == "logged_out":
            print("[whatsapp] sidecar reports the pairing was logged out.", file=sys.stderr)
            return
        if kind == "send_result":
            if not data.get("ok", True):
                print(f"[whatsapp] send failed: {data.get('error')}", file=sys.stderr)
            return
        if kind != "message":
            return

        if data.get("isGroup"):
            return   # v1: groups are ignored entirely

        sender_digits = _digits(data.get("senderNumber") or data.get("senderJid"))
        chat_jid = data.get("chatJid") or data.get("senderJid") or ""
        if sender_digits not in CONFIG.whatsapp_allowed_numbers:
            print(f"[whatsapp] blocked {data.get('senderJid')}: {(data.get('text') or '')[:80]}")
            return

        chan = WhatsAppChannel(self, chat_jid, sender_digits)
        text = (data.get("text") or "").strip()

        pending = self._pending.get(chat_jid)
        if pending is not None and not pending.done():
            word = text.lower()
            if word in YES_WORDS:
                pending.set_result(True)
            elif word in NO_WORDS:
                pending.set_result(False)
            else:
                await chan.send_text(
                    "⏳ I'm waiting for your YES or NO on the confirmation above — "
                    "other messages are paused until you answer or it times out."
                )
            return

        audio_paths = []
        audio_failed = False
        other = []
        media = data.get("media") or None
        if media:
            if media.get("kind") == "audio" and media.get("path"):
                audio_paths.append(Path(media["path"]))
            else:
                other.append(media.get("fileName") or f"{media.get('kind', 'file')}")

        await self.session.handle_message(
            IncomingMessage(
                channel=chan,
                text=text,
                audio_paths=audio_paths,
                audio_failed=audio_failed,
                other_attachments=other,
            )
        )

    # --------------------------------------------------------------- lifecycle

    def run(self):
        try:
            asyncio.run(self._supervise())
        except KeyboardInterrupt:
            self.stop()

    def stop(self):
        self._stopped = True
        if self._proc is not None and self._proc.returncode is None:
            with contextlib.suppress(Exception):
                self._proc.terminate()

    async def _supervise(self):
        node = shutil.which("node")
        if not node:
            print(
                "\n  [whatsapp] Node.js was not found on this machine, so the WhatsApp "
                "transport is DISABLED. Install Node.js 18+ and restart to enable it. "
                "Other transports are unaffected.\n",
                file=sys.stderr,
            )
            return
        if not (SIDECAR_DIR / "node_modules").exists():
            print(
                f"\n  [whatsapp] The sidecar's packages are not installed yet ({SIDECAR_DIR}). "
                "Run `npm install` inside the sidecar folder once, then restart. "
                "WhatsApp transport DISABLED for this run; other transports are unaffected.\n",
                file=sys.stderr,
            )
            return

        wa_dir = CONFIG.runtime_dir / "whatsapp"
        env = dict(
            os.environ,
            WA_BRIDGE_PORT=str(CONFIG.wa_bridge_port),
            WA_AUTH_DIR=str(wa_dir / "auth"),
            WA_MEDIA_DIR=str(wa_dir / "media"),
            WA_QR_FILE=str(wa_dir / "qr.txt"),
        )
        backoff = 2
        while not self._stopped:
            self._proc = await asyncio.create_subprocess_exec(
                node, str(SIDECAR_DIR / "index.js"),
                cwd=str(ROOT), env=env,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            )
            pump = asyncio.create_task(self._pump_output(self._proc))
            bridge = asyncio.create_task(self._bridge_loop(self._proc))
            rc = await self._proc.wait()
            bridge.cancel()
            pump.cancel()
            self._ws = None
            if self._stopped:
                return
            if rc == LOGGED_OUT_EXIT:
                print(
                    "\n  [whatsapp] The WhatsApp pairing was logged out (phone removed the "
                    "linked device, or the session expired). The transport will NOT restart "
                    "on its own: delete .runtime/whatsapp/auth, restart the agent, and scan "
                    "the new QR code. Other transports are unaffected.\n",
                    file=sys.stderr,
                )
                return
            print(f"[whatsapp] sidecar exited (code {rc}); restarting in {backoff}s...")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    async def _pump_output(self, proc):
        with contextlib.suppress(Exception):
            while True:
                line = await proc.stdout.readline()
                if not line:
                    return
                print(f"[whatsapp-sidecar] {line.decode(errors='replace').rstrip()}")

    async def _bridge_loop(self, proc):
        """Connect to the sidecar's bridge and dispatch until it dies."""
        import websockets

        url = f"ws://127.0.0.1:{CONFIG.wa_bridge_port}"
        while proc.returncode is None and not self._stopped:
            try:
                async with websockets.connect(url) as ws:
                    self._ws = ws
                    async for raw in ws:
                        try:
                            data = json.loads(raw)
                        except (TypeError, ValueError):
                            continue
                        # create_task: a long brain job must not block the
                        # bridge — a YES/NO reply has to get through while a
                        # confirmation is pending.
                        asyncio.create_task(self.handle_bridge_message(data))
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - sidecar not up yet / dropped: retry
                self._ws = None
                await asyncio.sleep(1)
