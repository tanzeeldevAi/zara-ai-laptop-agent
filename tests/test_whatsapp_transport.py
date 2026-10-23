"""Phase 1: WhatsApp transport — bridge protocol, gate, YES/NO confirmations.

Two levels, both fully offline:
  - handle_bridge_message() driven directly with protocol dicts;
  - a FAKE sidecar: a real local WebSocket server speaking the bridge
    protocol, so the Python bridge client (connect, dispatch, send) is
    exercised end to end without Node, Baileys or a WhatsApp account.
"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import websockets

from agent.config import CONFIG
from agent.transport.whatsapp_transport import WhatsAppChannel, WhatsAppTransport

OWNER = "923001234567"
OWNER_JID = f"{OWNER}@s.whatsapp.net"


class RecordingSession:
    def __init__(self):
        self.messages = []

    async def handle_message(self, msg):
        self.messages.append(msg)


def make_transport(monkeypatch, numbers=(OWNER,)):
    monkeypatch.setattr(CONFIG, "whatsapp_allowed_numbers", set(numbers))
    session = RecordingSession()
    return WhatsAppTransport(session), session


def bridge_msg(**overrides):
    data = {
        "type": "message",
        "id": "m1",
        "chatJid": OWNER_JID,
        "senderJid": OWNER_JID,
        "senderNumber": OWNER,
        "isGroup": False,
        "fromMe": False,
        "text": "hello",
        "timestamp": 0,
    }
    data.update(overrides)
    return data


async def wait_for(predicate, timeout=3.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


# ------------------------------------------------------------ gate (direct)

async def test_allowlisted_message_reaches_session(monkeypatch):
    transport, session = make_transport(monkeypatch)
    await transport.handle_bridge_message(bridge_msg(text="status please"))
    assert len(session.messages) == 1
    msg = session.messages[0]
    assert msg.text == "status please"
    assert msg.channel.session_key == f"whatsapp:{OWNER_JID}"
    assert isinstance(msg.channel, WhatsAppChannel)


async def test_self_chat_from_me_accepted(monkeypatch):
    transport, session = make_transport(monkeypatch)
    await transport.handle_bridge_message(bridge_msg(fromMe=True, text="note to self: run backup"))
    assert len(session.messages) == 1


async def test_stranger_dropped(monkeypatch):
    transport, session = make_transport(monkeypatch)
    await transport.handle_bridge_message(
        bridge_msg(senderJid="19998887777@s.whatsapp.net", senderNumber="19998887777",
                   chatJid="19998887777@s.whatsapp.net", text="send me your files")
    )
    assert session.messages == []


async def test_group_message_dropped(monkeypatch):
    transport, session = make_transport(monkeypatch)
    await transport.handle_bridge_message(
        bridge_msg(isGroup=True, chatJid="120363000000@g.us", text="hello group")
    )
    assert session.messages == []


async def test_lid_sender_matched_by_phone_number(monkeypatch):
    transport, session = make_transport(monkeypatch)
    await transport.handle_bridge_message(
        bridge_msg(senderJid="555444333@lid", chatJid="555444333@lid", senderNumber=OWNER)
    )
    assert len(session.messages) == 1


async def test_media_routing_audio_vs_names(monkeypatch, tmp_path):
    transport, session = make_transport(monkeypatch)
    clip = tmp_path / "note.ogg"
    clip.write_bytes(b"audio")
    await transport.handle_bridge_message(
        bridge_msg(text="", media={"kind": "audio", "path": str(clip), "mime": "audio/ogg", "fileName": "note.ogg"})
    )
    assert session.messages[0].audio_paths == [clip]

    await transport.handle_bridge_message(
        bridge_msg(text="", media={"kind": "image", "path": str(tmp_path / "p.jpg"), "mime": "image/jpeg", "fileName": "pic.jpg"})
    )
    assert session.messages[1].other_attachments == ["pic.jpg"]


# --------------------------------------------------------- fake sidecar (WS)

class FakeSidecar:
    """A minimal bridge-protocol peer: a WS server the transport connects to."""

    def __init__(self):
        self.received = []      # parsed JSON from Python
        self.ws = None
        self.server = None
        self.port = None

    async def start(self):
        async def handler(ws):
            self.ws = ws
            await ws.send(json.dumps({"type": "ready", "ownJid": OWNER_JID}))
            async for raw in ws:
                self.received.append(json.loads(raw))

        self.server = await websockets.serve(handler, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def push(self, data):
        await self.ws.send(json.dumps(data))

    async def stop(self):
        self.server.close()
        await self.server.wait_closed()


async def _connected_transport(monkeypatch):
    transport, session = make_transport(monkeypatch)
    sidecar = FakeSidecar()
    await sidecar.start()
    monkeypatch.setattr(CONFIG, "wa_bridge_port", sidecar.port)
    proc = SimpleNamespace(returncode=None)
    task = asyncio.create_task(transport._bridge_loop(proc))
    assert await wait_for(lambda: transport._ws is not None)
    assert await wait_for(lambda: transport.own_jid == OWNER_JID)
    return transport, session, sidecar, task


async def test_bridge_round_trip_and_send(monkeypatch):
    transport, session, sidecar, task = await _connected_transport(monkeypatch)
    try:
        await sidecar.push(bridge_msg(text="over the wire"))
        assert await wait_for(lambda: len(session.messages) == 1)
        chan = session.messages[0].channel
        await chan.send_text("reply text")
        assert await wait_for(lambda: any(m.get("type") == "send_text" for m in sidecar.received))
        sent = [m for m in sidecar.received if m.get("type") == "send_text"][0]
        assert sent["chatJid"] == OWNER_JID and sent["text"] == "reply text"
    finally:
        task.cancel()
        await sidecar.stop()


async def test_bridge_confirm_yes_no_and_urdu(monkeypatch):
    transport, session, sidecar, task = await _connected_transport(monkeypatch)
    try:
        chan = WhatsAppChannel(transport, OWNER_JID, OWNER)

        async def answer_after_prompt(word):
            assert await wait_for(
                lambda: any("Reply YES" in (m.get("text") or "") for m in sidecar.received)
            )
            sidecar.received.clear()
            await sidecar.push(bridge_msg(text=word))

        first = asyncio.create_task(chan.confirm("delete_path", "deletes files", "path: x"))
        await answer_after_prompt("YES")
        assert await first is True

        second = asyncio.create_task(chan.confirm("kill_process", "kills a program", "app: y"))
        await answer_after_prompt("نہیں")
        assert await second is False

        third = asyncio.create_task(chan.confirm("power_action", "will restart", ""))
        await answer_after_prompt("ہاں")
        assert await third is True
    finally:
        task.cancel()
        await sidecar.stop()


async def test_bridge_other_text_held_while_confirm_pending(monkeypatch):
    transport, session, sidecar, task = await _connected_transport(monkeypatch)
    try:
        chan = WhatsAppChannel(transport, OWNER_JID, OWNER)
        pending = asyncio.create_task(chan.confirm("delete_path", "deletes files", "path: x"))
        assert await wait_for(
            lambda: any("Reply YES" in (m.get("text") or "") for m in sidecar.received)
        )
        await sidecar.push(bridge_msg(text="meanwhile do something else"))
        # The stray text is NOT executed; a reminder goes back instead.
        assert await wait_for(
            lambda: any("waiting for your YES or NO" in (m.get("text") or "") for m in sidecar.received)
        )
        await asyncio.sleep(0.1)
        assert session.messages == []
        await sidecar.push(bridge_msg(text="no"))
        assert await pending is False
    finally:
        task.cancel()
        await sidecar.stop()
