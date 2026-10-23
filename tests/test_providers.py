"""(b) Provider registry: sane capability flags + LiteLLM model strings."""

import pytest

from agent.brain.providers import (
    PROVIDERS,
    build_model_string,
    get_provider,
    model_supports_vision,
)


def test_all_five_providers_registered():
    assert set(PROVIDERS) == {"anthropic", "openai", "gemini", "deepseek", "custom"}


def test_capability_flags_sane():
    assert get_provider("anthropic").supports_vision is True
    assert get_provider("anthropic").supports_tools is True
    assert get_provider("openai").supports_vision is True
    assert get_provider("gemini").supports_vision is True
    # DeepSeek's API models are text-only for vision; the whole point of the
    # flag is that this is declared, not discovered by failure.
    assert get_provider("deepseek").supports_vision is False
    assert get_provider("deepseek").supports_tools is True
    # Unknown endpoint: conservative on vision.
    assert get_provider("custom").supports_vision is False


def test_every_provider_has_key_and_display():
    for provider in PROVIDERS.values():
        assert provider.display_name
        assert provider.env_key  # each route knows which env var holds its key


@pytest.mark.parametrize(
    "provider_id,model,expected",
    [
        ("anthropic", "claude-opus-5", "anthropic/claude-opus-5"),
        ("openai", "gpt-5", "openai/gpt-5"),
        ("gemini", "gemini-2.5-pro", "gemini/gemini-2.5-pro"),
        ("deepseek", "deepseek-chat", "deepseek/deepseek-chat"),
        ("custom", "llama-3.3-70b-versatile", "openai/llama-3.3-70b-versatile"),
        # already-prefixed strings pass through untouched
        ("anthropic", "anthropic/claude-sonnet-5", "anthropic/claude-sonnet-5"),
        ("custom", "openrouter/anthropic/claude-sonnet-5", "openrouter/anthropic/claude-sonnet-5"),
        ("custom", "ollama/llama3.1", "ollama/llama3.1"),
    ],
)
def test_build_model_string(provider_id, model, expected):
    assert build_model_string(get_provider(provider_id), model) == expected


def test_default_model_used_when_blank():
    assert build_model_string(get_provider("anthropic"), "") == "anthropic/claude-opus-5"
    assert build_model_string(get_provider("deepseek"), "") == "deepseek/deepseek-chat"


def test_model_level_vision():
    assert model_supports_vision(get_provider("deepseek"), "deepseek-chat") is False
    assert model_supports_vision(get_provider("deepseek"), "deepseek-reasoner") is False
    assert model_supports_vision(get_provider("openai"), "gpt-5") is True
    assert model_supports_vision(get_provider("anthropic"), "anything") is True


def test_unknown_provider_is_none():
    assert get_provider("not-a-provider") is None
