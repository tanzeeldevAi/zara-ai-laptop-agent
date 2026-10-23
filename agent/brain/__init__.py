"""Brain seam: anything that turns an instruction into tool calls + a reply.

A brain implements:
    async handle(key, user_text, chan)   run one instruction to completion
    reset(key)                           forget the conversation for `key`
    model_label                          human-readable model description

Two brains ship today: ApiBrain (LiteLLM, any provider) and SdkBrain
(Claude subscription via the Claude Code SDK). Both execute tools only
through agent.safety.ToolExecutor, so the safety pipeline applies equally.
"""

from typing import Protocol, runtime_checkable


@runtime_checkable
class Brain(Protocol):
    async def handle(self, key, user_text, chan): ...
    def reset(self, key): ...


def make_brain():
    """Construct the configured brain. Returns (brain, label)."""
    from ..config import effective_brain

    if effective_brain() == "subscription":
        from .sdk_brain import SdkBrain

        return SdkBrain(), "subscription (Claude Code login)"
    from .api_brain import ApiBrain

    return ApiBrain(), "API key"


__all__ = ["Brain", "make_brain"]
