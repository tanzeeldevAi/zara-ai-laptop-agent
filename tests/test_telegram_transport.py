"""Phase 1: Telegram transport — allowlist, callbacks, downloads, routing.

Everything is faked at the bot/object level: no network, no real bot token.
The handler methods are called with duck-typed update/context objects, the
same shapes python-telegram-bot passes in production.
"""

import asyncio
from pathlib import Path
from types import SimpleNamespace

from agent.config import CONFIG
from agent.transport.telegram_transport import TelegramChannel, TelegramTransport


class FakeSentMessage:
    def __init__(self):
        self.edits = []

    async def edit_text(self, text, reply_markup=None):
        self.edits.append(text)


class FakeBot:
    def __init__(self):
        self.messages = []        # (chat_id, text, reply_markup, FakeSentMessage)
        self.files = []           # (kind, chat_id)
        self.actions = []

    async def send_message(self, chat_id, text, reply_markup=None):
        sent = FakeSentMessage()
        self.messages.append((chat_id, text, reply_markup, sent))
        return sent

    async def _record_file(self, kind, chat_id, fh):
        self.files.append((kind, chat_id, getattr(fh, "name", "")))

    async def send_photo(self, chat_id, photo, caption=None):
        await self._record_file("photo", chat_id, photo)

    async def send_voice(self, chat_id, voice, caption=None):
        await self._record_file("voice", chat_id, voice)

    async def send_audio(self, chat_id, audio, caption=None):
        await self._record_file("audio", chat_id, audio)

    async def send_document(self, chat_id, document, caption=None):
        await self._record_file("document", chat_id, document)

    async def send_chat_action(self, chat_id, action):
        self.actions.append((chat_id, action))


class FakeTGFile:
    async def download_to_drive(self, path):
        Path(path).write_bytes(b"fake-audio")


class FakeVoice:
    file_unique_id = "vq1"

    async def get_file(self):
        return FakeTGFile()


class FakeMessage:
    def __init__(self, user_id=42, chat_id=99, text="hello", chat_type="private",
                 voice=None, photo=None, document=None, caption=None):
        self.from_user = SimpleNamespace(id=user_id, is_bot=False, username="owner")
        self.chat = SimpleNamespace(id=chat_id, type=chat_type)
        self.chat_id = chat_id
        self.text = text
        self.caption = caption
        self.voice = voice
        self.audio = None
        self.photo = photo
        self.document = document
        self.replies = []

    async def reply_text(self, text):
        self.replies.append(text)


class FakeQuery:
    def __init__(self, data, user_id):
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


class RecordingSession:
    def __init__(self):
        self.messages = []

    async def handle_message(self, msg):
        self.messages.append(msg)


def update_of(message):
    return SimpleNamespace(message=message, edited_message=None, callback_query=None)


def context_of(bot):
    return SimpleNamespace(bot=bot)


def make_transport(monkeypatch, allowed=(42,)):
    monkeypatch.setattr(CONFIG, "telegram_allowed_users", set(allowed))
    session = RecordingSession()
    return TelegramTransport(session), session, FakeBot()


# ------------------------------------------------------------------ intake

async def test_allowlisted_message_reaches_session(monkeypatch):
    transport, session, bot = make_transport(monkeypatch)
    await transport._on_message(update_of(FakeMessage(text="status please")), context_of(bot))
    assert len(session.messages) == 1
    msg = session.messages[0]
    assert msg.text == "status please"
    assert msg.channel.session_key == "telegram:99"
    assert isinstance(msg.channel, TelegramChannel)


async def test_stranger_refused_in_private_silent_in_group(monkeypatch):
    transport, session, bot = make_transport(monkeypatch)
    private = FakeMessage(user_id=777, text="rm -rf /")
    await transport._on_message(update_of(private), context_of(bot))
    assert private.replies and "not authorised" in private.replies[0]
    group = FakeMessage(user_id=777, chat_type="group", text="hi all")
    await transport._on_message(update_of(group), context_of(bot))
    assert group.replies == []
    assert session.messages == []


async def test_voice_note_downloaded_and_handed_to_session(monkeypatch):
    transport, session, bot = make_transport(monkeypatch)
    msg = FakeMessage(text=None, voice=FakeVoice())
    await transport._on_message(update_of(msg), context_of(bot))
    assert len(session.messages) == 1
    incoming = session.messages[0]
    assert len(incoming.audio_paths) == 1
    assert incoming.audio_paths[0].exists()
    incoming.audio_paths[0].unlink()


async def test_photo_and_document_surface_by_name(monkeypatch):
    transport, session, bot = make_transport(monkeypatch)
    msg = FakeMessage(
        text=None,
        photo=[SimpleNamespace(file_unique_id="p1")],
        document=SimpleNamespace(file_name="report.pdf"),
    )
    await transport._on_message(update_of(msg), context_of(bot))
    assert session.messages[0].other_attachments == ["photo.jpg", "report.pdf"]


# ------------------------------------------------------------------ sending

async def test_send_file_routes_by_extension(tmp_path):
    bot = FakeBot()
    chan = TelegramChannel(bot, 99, 42)
    for name in ("shot.png", "clip.ogg", "reply.mp3", "doc.pdf"):
        path = tmp_path / name
        path.write_bytes(b"x")
        await chan.send_file(path)
    kinds = [kind for kind, _, _ in bot.files]
    assert kinds == ["photo", "voice", "audio", "document"]


async def test_send_text_chunks_long_messages():
    bot = FakeBot()
    chan = TelegramChannel(bot, 99, 42)
    await chan.send_text("x" * 9000)
    assert len(bot.messages) >= 3
    assert all(len(text) <= 4000 for _, text, _, _ in bot.messages)


# ------------------------------------------------------------- confirmations

def _callback_data(bot, button_index=0):
    _, _, markup, _ = bot.messages[-1]
    return markup.inline_keyboard[0][button_index].callback_data


async def _start_confirm(bot, transport):
    chan = TelegramChannel(bot, 99, 42, transport=transport)
    task = asyncio.create_task(chan.confirm("delete_path", "deletes files", "path: C:\\x"))
    await asyncio.sleep(0.05)
    return task


async def test_confirm_approve_via_callback(monkeypatch):
    transport, _, bot = make_transport(monkeypatch)
    task = await _start_confirm(bot, transport)
    query = FakeQuery(_callback_data(bot, 0), user_id=42)
    await transport._on_callback(SimpleNamespace(callback_query=query), None)
    assert await task is True


async def test_confirm_reject_via_callback(monkeypatch):
    transport, _, bot = make_transport(monkeypatch)
    task = await _start_confirm(bot, transport)
    query = FakeQuery(_callback_data(bot, 1), user_id=42)
    await transport._on_callback(SimpleNamespace(callback_query=query), None)
    assert await task is False


async def test_confirm_stranger_tap_refused_and_pending_survives(monkeypatch):
    transport, _, bot = make_transport(monkeypatch)
    task = await _start_confirm(bot, transport)
    data = _callback_data(bot, 0)
    stranger = FakeQuery(data, user_id=999)
    await transport._on_callback(SimpleNamespace(callback_query=stranger), None)
    assert stranger.answers and stranger.answers[0][1] is True   # show_alert=True
    assert not task.done()                                       # still waiting for the owner
    owner = FakeQuery(data, user_id=42)
    await transport._on_callback(SimpleNamespace(callback_query=owner), None)
    assert await task is True


async def test_confirm_expired_callback_answered(monkeypatch):
    transport, _, bot = make_transport(monkeypatch)
    query = FakeQuery("confirm:deadbeef00:yes", user_id=42)
    await transport._on_callback(SimpleNamespace(callback_query=query), None)
    assert query.answers and "expired" in (query.answers[0][0] or "")
