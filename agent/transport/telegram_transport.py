"""Telegram transport: the Bot API front end, behind the Transport seam.

Long polling only — the laptop connects outward to Telegram, exactly like
Discord; no webhook, no open ports. Library: python-telegram-bot (>=21).

Behaviour mirrors the Discord transport:
  - the allowlist gate runs here, before anything reaches the session;
  - voice notes are downloaded here and handed to the session as file paths;
  - photos/documents are surfaced by name only (the session contract);
  - confirmations use native inline-keyboard buttons, and only the user who
    was asked can answer them — a stranger's tap is refused and logged.
"""

import asyncio
import contextlib
import sys
import time
import uuid
from pathlib import Path

from ..config import CONFIG
from . import IncomingMessage

TELEGRAM_MSG_LIMIT = 4000          # Telegram's hard limit is 4096
CONFIRM_TIMEOUT = 180             # seconds, same as the Discord buttons
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp")
VOICE_EXTS = (".ogg", ".oga", ".opus")
AUDIO_EXTS = (".mp3", ".m4a", ".wav", ".flac", ".aac") + VOICE_EXTS


def chunks(text, size):
    """Split text for Telegram's message limit, preferring line breaks."""
    text = str(text).rstrip()
    while text:
        if len(text) <= size:
            yield text
            return
        cut = text.rfind("\n", 0, size)
        if cut < size // 3:
            cut = size
        yield text[:cut]
        text = text[cut:].lstrip("\n")


class _Typing:
    """Async context manager: show Telegram's 'typing…' for the job's start."""

    def __init__(self, channel):
        self.channel = channel

    async def __aenter__(self):
        with contextlib.suppress(Exception):
            await self.channel.bot.send_chat_action(
                chat_id=self.channel.chat_id, action="typing"
            )
        return self

    async def __aexit__(self, *exc):
        return False


class TelegramChannel:
    """What the brains are allowed to do to a Telegram chat."""

    def __init__(self, bot, chat_id, user_id, transport=None):
        self.bot = bot
        self.chat_id = chat_id
        self.user_id = user_id
        self.transport = transport     # confirm registry lives on the transport
        self.last_spoken = ""
        self.spoke = False

    @property
    def channel_id(self):
        return self.chat_id

    @property
    def session_key(self):
        return f"telegram:{self.chat_id}"

    def typing(self):
        return _Typing(self)

    async def send_text(self, text):
        text = str(text)
        if text.strip():
            self.last_spoken = text
        for part in chunks(text, TELEGRAM_MSG_LIMIT):
            try:
                await self.bot.send_message(chat_id=self.chat_id, text=part)
            except Exception as exc:  # noqa: BLE001 - a failed send must not kill the turn
                print(f"[telegram] send failed: {exc}", file=sys.stderr)

    async def note(self, text):
        try:
            await self.bot.send_message(chat_id=self.chat_id, text=f"⚙️ {str(text)[:400]}")
        except Exception:  # noqa: BLE001
            pass

    async def send_file(self, path, caption=None):
        target = Path(path)
        if not target.exists():
            return
        suffix = target.suffix.lower()
        try:
            with target.open("rb") as fh:
                if suffix in IMAGE_EXTS:
                    await self.bot.send_photo(chat_id=self.chat_id, photo=fh, caption=caption)
                elif suffix in VOICE_EXTS:
                    await self.bot.send_voice(chat_id=self.chat_id, voice=fh, caption=caption)
                elif suffix in AUDIO_EXTS:
                    await self.bot.send_audio(chat_id=self.chat_id, audio=fh, caption=caption)
                else:
                    await self.bot.send_document(chat_id=self.chat_id, document=fh, caption=caption)
        except Exception as exc:  # noqa: BLE001
            await self.send_text(f"Couldn't upload `{target.name}`: {exc}")

    async def send_voice(self, path, caption=None):
        target = Path(path)
        if not target.exists():
            return
        try:
            with target.open("rb") as fh:
                if target.suffix.lower() in VOICE_EXTS:
                    await self.bot.send_voice(chat_id=self.chat_id, voice=fh, caption=caption)
                else:
                    await self.bot.send_audio(chat_id=self.chat_id, audio=fh, caption=caption)
        except Exception as exc:  # noqa: BLE001
            await self.send_text(f"Couldn't upload `{target.name}`: {exc}")

    async def confirm(self, tool_name, reason, detail):
        """Ask with Approve/Reject inline buttons; only the asked user counts."""
        if self.transport is None:
            return False
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        cid = uuid.uuid4().hex[:12]
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self.transport.register_confirm(cid, self.user_id, future)

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ Run it", callback_data=f"confirm:{cid}:yes"),
                    InlineKeyboardButton("❌ Cancel", callback_data=f"confirm:{cid}:no"),
                ]
            ]
        )
        text = (
            f"⚠️ Confirm before I run this\n\n{tool_name} — {reason}\n\n"
            f"{detail[:900]}\n\nAuto-cancels in 3 minutes if you don't answer."
        )
        try:
            message = await self.bot.send_message(
                chat_id=self.chat_id, text=text, reply_markup=keyboard
            )
        except Exception:  # noqa: BLE001
            self.transport.drop_confirm(cid)
            return False

        try:
            approved = await asyncio.wait_for(future, CONFIRM_TIMEOUT)
        except asyncio.TimeoutError:
            approved = False
        finally:
            self.transport.drop_confirm(cid)

        with contextlib.suppress(Exception):
            await message.edit_text(
                text + ("\n\n✅ Approved" if approved else "\n\n❌ Cancelled"),
                reply_markup=None,
            )
        return bool(approved)


class TelegramTransport:
    """Owns the Telegram polling lifecycle."""

    name = "telegram"

    def __init__(self, session):
        self.session = session
        self.app = None
        self._confirms = {}   # cid -> (user_id, future)

    # ---- confirm registry (used by TelegramChannel + the callback handler) ----

    def register_confirm(self, cid, user_id, future):
        self._confirms[cid] = (user_id, future)

    def drop_confirm(self, cid):
        self._confirms.pop(cid, None)

    async def _on_callback(self, update, context):
        query = update.callback_query
        if query is None:
            return
        data = query.data or ""
        parts = data.split(":")
        if len(parts) != 3 or parts[0] != "confirm":
            await query.answer()
            return
        _, cid, answer = parts
        pending = self._confirms.get(cid)
        if pending is None:
            await query.answer("This confirmation has expired.", show_alert=True)
            return
        owner_id, future = pending
        if query.from_user is None or query.from_user.id != owner_id:
            # Only the user who was asked may answer — anyone else is refused.
            print(f"[telegram] blocked confirm tap by {getattr(query.from_user, 'id', '?')}")
            await query.answer("Not your agent.", show_alert=True)
            return
        await query.answer()
        if not future.done():
            future.set_result(answer == "yes")

    # ---- message intake ----

    async def _on_message(self, update, context):
        message = update.message or update.edited_message
        if message is None or message.from_user is None:
            return
        if message.from_user.is_bot:
            return

        allowed = message.from_user.id in CONFIG.telegram_allowed_users
        try:
            (CONFIG.runtime_dir / "last_sender_telegram.txt").write_text(
                f"id={message.from_user.id}\nname={message.from_user.username or ''}\nallowed={allowed}\n",
                encoding="utf-8",
            )
        except Exception:  # noqa: BLE001
            pass
        if not allowed:
            chat_type = getattr(message.chat, "type", "")
            if chat_type == "private":
                with contextlib.suppress(Exception):
                    await message.reply_text("You are not authorised to control this machine.")
            print(f"[telegram] blocked {message.from_user.id}: {(message.text or '')[:80]}")
            return

        bot = context.bot if context is not None else self.app.bot
        chan = TelegramChannel(bot, message.chat.id, message.from_user.id, transport=self)

        audio_paths = []
        audio_failed = False
        other = []

        voice_note = getattr(message, "voice", None) or getattr(message, "audio", None)
        if voice_note is not None:
            try:
                tg_file = await voice_note.get_file()
                path = CONFIG.voice_dir / f"in-{int(time.time() * 1000)}-{voice_note.file_unique_id}.ogg"
                await tg_file.download_to_drive(str(path))
                audio_paths.append(path)
            except Exception as exc:  # noqa: BLE001
                audio_failed = True
                await chan.send_text(f"Couldn't download the voice note: {exc}")
        if getattr(message, "photo", None):
            other.append("photo.jpg")
        document = getattr(message, "document", None)
        if document is not None:
            other.append(document.file_name or "document")

        text = (message.text or message.caption or "").strip()
        await self.session.handle_message(
            IncomingMessage(
                channel=chan,
                text=text,
                audio_paths=audio_paths,
                audio_failed=audio_failed,
                other_attachments=other,
            )
        )

    # ---- lifecycle ----

    def run(self):
        from telegram.ext import Application, CallbackQueryHandler, MessageHandler, filters

        app = Application.builder().token(CONFIG.telegram_token).build()
        self.app = app
        app.add_handler(CallbackQueryHandler(self._on_callback))
        app.add_handler(MessageHandler(filters.ALL, self._on_message))
        app.add_error_handler(self._on_error)

        print(f"\n  {CONFIG.product_name} — Telegram transport starting (long polling)")
        print(f"  authorised Telegram user IDs: {', '.join(str(i) for i in CONFIG.telegram_allowed_users)}")
        # run_polling raises on an invalid token / network death; the caller
        # (composition root thread) logs it and the other transports live on.
        app.run_polling(drop_pending_updates=True)

    @staticmethod
    async def _on_error(update, context):
        print(f"[telegram] update error: {context.error}", file=sys.stderr)
