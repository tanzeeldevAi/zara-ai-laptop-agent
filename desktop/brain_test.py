"""The wizard's "Test connection" check (Qt-free).

Runs ONE tiny completion through LiteLLM so the user learns immediately
whether provider + key + model actually work, instead of finding out after
the agent starts. The completion callable is injectable — unit tests fake
it, the GUI uses the real one, and a real call only ever happens when the
user clicks the button (nothing here runs at import or page load).

Subscription mode can't be probed with a cheap API call, so it checks for
the Claude Code CLI instead and says exactly that.
"""

from agent import config as agent_config
from agent.brain.providers import build_model_string, get_provider


def default_completion(**kwargs):
    """The real one-call probe. Imported lazily so tests never load LiteLLM."""
    import litellm

    return litellm.completion(**kwargs)


def test_brain_connection(settings, *, completion=None):
    """Return (ok: bool, message: str) for the brain in `settings`."""
    mode = settings._effective_brain()
    if mode == "subscription":
        if agent_config._claude_cli_present():
            return True, (
                "Claude Code CLI found — subscription mode is ready. "
                "(No API call was made; subscription mode has no per-token cost.)"
            )
        return False, (
            "Claude Code CLI not found. Install it with "
            "`npm i -g @anthropic-ai/claude-code` and run `claude` once to sign in."
        )

    provider = get_provider(settings.api_provider)
    if provider is None:
        return False, f"Unknown provider '{settings.api_provider}'."
    if provider.id == "custom" and not settings.custom_api_base:
        return False, "Set the custom endpoint's base URL first."
    if not settings.provider_model:
        return False, "Set a model name first."

    model = build_model_string(provider, settings.provider_model)
    kwargs = {
        "model": model,
        "messages": [{"role": "user", "content": "Reply with exactly: OK"}],
        "max_tokens": 16,
    }
    key = settings._provider_key()
    if key:
        kwargs["api_key"] = key
    if provider.id == "custom":
        kwargs["api_base"] = settings.custom_api_base

    completion = completion or default_completion
    try:
        response = completion(**kwargs)
    except Exception as exc:  # noqa: BLE001 - surface the provider's own words
        return False, f"{provider.display_name} call failed: {type(exc).__name__}: {exc}"
    try:
        text = (response.choices[0].message.content or "").strip()
    except Exception:  # noqa: BLE001
        text = ""
    return True, f"{provider.display_name} answered via {model}: {text[:60]!r} — connection works."
