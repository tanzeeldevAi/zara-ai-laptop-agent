"""Phase 4: session enforcement — activation mode never reaches the brain.
"""

import pytest

from agent import licensing
from agent.session import AgentSession
from agent.store import Store
from agent.transport import IncomingMessage
from tests.fakes import FakeChannel


class StubBrain:
    def __init__(self):
        self.handled = []

    async def handle(self, key, text, chan):
        self.handled.append((key, text))
        await chan.send_text(f"done: {text}")

    def reset(self, key):
        pass


class FakeManager:
    def __init__(self, status):
        self._status = status

    def status(self):
        return self._status


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "memory.db")


def make_chan():
    chan = FakeChannel(user_id=1, channel_id=7)
    chan.session_key = "fake:7"
    return chan


def make_session(store, status):
    brain = StubBrain()
    session = AgentSession(brain, "stub", store=store,
                           license_manager=FakeManager(status))
    return session, brain


async def test_expired_license_answers_notice_only(store):
    session, brain = make_session(
        store, licensing.LicenseStatus("expired", detail="period ended"))
    chan = make_chan()
    await session.handle_message(IncomingMessage(channel=chan, text="open chrome"))
    assert brain.handled == []
    assert any("needs activation" in text for text in chan.texts)
    # nothing was queued either
    assert store.list_tasks() == []


async def test_trial_expired_and_revoked_also_gate(store):
    for state in ("trial_expired", "revoked", "invalid"):
        session, brain = make_session(store, licensing.LicenseStatus(state))
        chan = make_chan()
        await session.handle_message(IncomingMessage(channel=chan, text="hello"))
        assert brain.handled == [], state
        assert any("activation" in text for text in chan.texts), state


async def test_license_command_works_in_activation_mode(store):
    session, brain = make_session(
        store, licensing.LicenseStatus("trial_expired", days_left=0))
    chan = make_chan()
    await session.handle_message(IncomingMessage(channel=chan, text="/license"))
    assert brain.handled == []
    assert any("trial_expired" in text for text in chan.texts)


async def test_license_command_reports_when_active(store):
    session, brain = make_session(
        store, licensing.LicenseStatus("active", plan="pro",
                                       buyer_email="buyer@example.com"))
    chan = make_chan()
    await session.handle_message(IncomingMessage(channel=chan, text="/license"))
    assert brain.handled == []
    assert any("pro" in text and "buyer@example.com" in text for text in chan.texts)


async def test_active_and_trial_and_dev_reach_the_brain(store):
    for state in ("active", "trial", "grace", "development"):
        session, brain = make_session(store, licensing.LicenseStatus(state))
        chan = make_chan()
        await session.handle_message(IncomingMessage(channel=chan, text="do it"))
        assert [text for _key, text in brain.handled] == ["do it"], state


async def test_queued_task_is_gated_when_license_lapses(store):
    session, brain = make_session(
        store, licensing.LicenseStatus("expired"))
    chan = make_chan()
    row = {"session_key": "fake:7", "prompt": "scheduled thing"}
    await session._run_task(row, chan, None)
    assert brain.handled == []
    assert any("needs activation" in text for text in chan.texts)
