"""Phase 1: session-key namespacing.

Two transports can hand the session the same numeric chat id; their brain
memory, jobs and voice state must never collide. Channels expose a
`session_key` ("telegram:7") and the session keys everything by it.
"""

from types import SimpleNamespace

from agent.session import AgentSession
from agent.transport import IncomingMessage, session_key_of
from agent.transport.discord_transport import DiscordChannel
from tests.fakes import FakeChannel


class StubBrain:
    def __init__(self):
        self.sessions = {}
        self.handled = []

    async def handle(self, key, text, chan):
        self.handled.append(key)
        self.sessions.setdefault(key, []).append(text)

    def reset(self, key):
        self.sessions.pop(key, None)


def named_channel(prefix, channel_id=7):
    chan = FakeChannel(user_id=1, channel_id=channel_id)
    chan.session_key = f"{prefix}:{channel_id}"
    return chan


def test_session_key_of_fallback_and_namespace():
    assert session_key_of(FakeChannel(channel_id=7)) == 7
    assert session_key_of(named_channel("telegram")) == "telegram:7"


def test_discord_channel_namespaces_its_key():
    chan = DiscordChannel(SimpleNamespace(id=555), user_id=42)
    assert chan.channel_id == 555
    assert chan.session_key == "discord:555"


async def test_same_chat_id_two_transports_two_sessions():
    brain = StubBrain()
    session = AgentSession(brain, "stub")
    await session.handle_message(IncomingMessage(channel=named_channel("telegram"), text="hello tg"))
    await session.handle_message(IncomingMessage(channel=named_channel("discord"), text="hello dc"))
    assert brain.handled == ["telegram:7", "discord:7"]
    assert brain.sessions["telegram:7"] == ["hello tg"]
    assert brain.sessions["discord:7"] == ["hello dc"]


async def test_new_resets_only_its_own_transport():
    brain = StubBrain()
    session = AgentSession(brain, "stub")
    tg = named_channel("telegram")
    dc = named_channel("discord")
    await session.handle_message(IncomingMessage(channel=tg, text="a"))
    await session.handle_message(IncomingMessage(channel=dc, text="b"))
    await session.handle_message(IncomingMessage(channel=tg, text="/new"))
    assert "telegram:7" not in brain.sessions
    assert brain.sessions["discord:7"] == ["b"]
