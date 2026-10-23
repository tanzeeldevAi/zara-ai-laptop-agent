"""(e) Transport contract: a FakeTransport drives one instruction through the
API brain (LiteLLM mocked — no real API calls, no Discord connection) and the
final text lands back on the channel.
"""

from types import SimpleNamespace

from agent.brain.api_brain import ApiBrain
from agent.config import CONFIG
from agent.session import AgentSession
from agent.transport import IncomingMessage, TransportChannel
from tests.fakes import FakeChannel


class FakeTransport:
    """The smallest possible transport: hand the session an IncomingMessage."""

    name = "fake"

    def __init__(self, session):
        self.session = session
        self.channel = FakeChannel()

    async def deliver(self, text):
        await self.session.handle_message(
            IncomingMessage(channel=self.channel, text=text)
        )


def completion(content="", tool_calls=None):
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def tool_call(call_id, name, arguments="{}"):
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def make_brain(monkeypatch):
    monkeypatch.setattr(CONFIG, "api_provider", "anthropic")
    monkeypatch.setattr(CONFIG, "_provider_model_raw", "")
    monkeypatch.setattr(CONFIG, "model", "claude-opus-5")
    return ApiBrain()


def test_fake_channel_satisfies_protocol():
    assert isinstance(FakeChannel(), TransportChannel)


async def test_plain_instruction_round_trip(monkeypatch):
    async def fake_acompletion(**kwargs):
        # The system prompt and the tools must reach the provider call.
        assert kwargs["messages"][0]["role"] == "system"
        assert kwargs["model"] == "anthropic/claude-opus-5"
        assert len(kwargs["tools"]) == 49   # 38 original + 11 Phase 3 tools
        return completion(content="All done.")

    monkeypatch.setattr("agent.brain.api_brain.litellm.acompletion", fake_acompletion)

    transport = FakeTransport(AgentSession(make_brain(monkeypatch), "API key"))
    await transport.deliver("say something")

    assert "All done." in transport.channel.texts


async def test_tool_call_round_trip(monkeypatch):
    seen = {"calls": 0}

    async def fake_acompletion(**kwargs):
        seen["calls"] += 1
        if seen["calls"] == 1:
            return completion(tool_calls=[tool_call("t1", "system_status")])
        # Second call must carry the tool result back to the model.
        roles = [m["role"] for m in kwargs["messages"]]
        assert "tool" in roles
        return completion(content="Status reported.")

    monkeypatch.setattr("agent.brain.api_brain.litellm.acompletion", fake_acompletion)

    transport = FakeTransport(AgentSession(make_brain(monkeypatch), "API key"))
    await transport.deliver("how is the laptop?")

    assert seen["calls"] == 2
    assert "Status reported." in transport.channel.texts


async def test_no_vision_model_gets_loud_notice(monkeypatch):
    captured = {}

    async def fake_acompletion(**kwargs):
        captured["system"] = kwargs["messages"][0]["content"]
        return completion(content="Understood.")

    monkeypatch.setattr("agent.brain.api_brain.litellm.acompletion", fake_acompletion)
    monkeypatch.setattr(CONFIG, "api_provider", "deepseek")
    monkeypatch.setattr(CONFIG, "_provider_model_raw", "")
    monkeypatch.setattr(CONFIG, "custom_model", "")
    brain = ApiBrain()
    assert brain.supports_vision is False

    transport = FakeTransport(AgentSession(brain, "API key"))
    await transport.deliver("look at my screen")

    # The model is told, in its system prompt, that it cannot see images —
    # and the user still gets a normal reply. No silent failure.
    assert "cannot see images" in captured["system"]
    assert "Understood." in transport.channel.texts


async def test_help_command_flows_through_session(monkeypatch):
    transport = FakeTransport(AgentSession(make_brain(monkeypatch), "API key"))
    await transport.deliver("/help")
    assert any("/stop" in text for text in transport.channel.texts)


async def test_new_command_resets_brain(monkeypatch):
    brain = make_brain(monkeypatch)
    brain.sessions[7] = [{"role": "user", "content": "old"}]
    transport = FakeTransport(AgentSession(brain, "API key"))
    await transport.deliver("/new")
    assert 7 not in brain.sessions
    assert any("Fresh start" in text for text in transport.channel.texts)
