"""(c) Config validation: setup problems and the vision warning.

CONFIG is a module-level singleton built from the environment at import time,
so tests monkeypatch its attributes (and the module's helpers) directly.
"""

from agent import config as config_mod

CONFIG = config_mod.CONFIG


def test_branding_defaults():
    assert CONFIG.product_name == "CodeHubb Assistant"
    assert CONFIG.assistant_name  # persona name present (default "Zara")
    assert CONFIG.agent_name == CONFIG.assistant_name


def test_missing_token_and_allowlist_flagged(monkeypatch):
    monkeypatch.setattr(CONFIG, "discord_token", "")
    monkeypatch.setattr(CONFIG, "allowed_users", set())
    monkeypatch.setattr(CONFIG, "brain", "subscription")
    monkeypatch.setattr(config_mod, "_claude_cli_present", lambda: True)
    problems = config_mod.validate()
    assert any("DISCORD_BOT_TOKEN" in p for p in problems)
    assert any("DISCORD_ALLOWED_USER_IDS" in p for p in problems)


def test_api_anthropic_without_key_flagged(monkeypatch):
    monkeypatch.setattr(CONFIG, "discord_token", "x")
    monkeypatch.setattr(CONFIG, "allowed_users", {1})
    monkeypatch.setattr(CONFIG, "brain", "api")
    monkeypatch.setattr(CONFIG, "api_provider", "anthropic")
    monkeypatch.setattr(CONFIG, "anthropic_key", "")
    problems = config_mod.validate()
    assert any("ANTHROPIC_API_KEY" in p for p in problems)


def test_api_openai_without_key_names_its_own_env_var(monkeypatch):
    monkeypatch.setattr(CONFIG, "discord_token", "x")
    monkeypatch.setattr(CONFIG, "allowed_users", {1})
    monkeypatch.setattr(CONFIG, "brain", "api")
    monkeypatch.setattr(CONFIG, "api_provider", "openai")
    monkeypatch.setattr(CONFIG, "provider_key", lambda: "")
    problems = config_mod.validate()
    assert any("OPENAI_API_KEY" in p for p in problems)


def test_custom_without_base_or_model_flagged(monkeypatch):
    monkeypatch.setattr(CONFIG, "discord_token", "x")
    monkeypatch.setattr(CONFIG, "allowed_users", {1})
    monkeypatch.setattr(CONFIG, "brain", "api")
    monkeypatch.setattr(CONFIG, "api_provider", "custom")
    monkeypatch.setattr(CONFIG, "custom_api_base", "")
    monkeypatch.setattr(CONFIG, "_provider_model_raw", "")
    monkeypatch.setattr(CONFIG, "custom_model", "")
    problems = config_mod.validate()
    assert any("CUSTOM_API_BASE" in p for p in problems)
    assert any("CUSTOM_MODEL" in p or "PROVIDER_MODEL" in p for p in problems)


def test_unknown_provider_flagged(monkeypatch):
    monkeypatch.setattr(CONFIG, "discord_token", "x")
    monkeypatch.setattr(CONFIG, "allowed_users", {1})
    monkeypatch.setattr(CONFIG, "brain", "api")
    monkeypatch.setattr(CONFIG, "api_provider", "bogus")
    problems = config_mod.validate()
    assert any("not a known provider" in p for p in problems)


def test_anthropic_model_backward_compatible(monkeypatch):
    # An old .env with only AGENT_MODEL must drive the API brain as before.
    monkeypatch.setattr(CONFIG, "api_provider", "anthropic")
    monkeypatch.setattr(CONFIG, "_provider_model_raw", "")
    monkeypatch.setattr(CONFIG, "model", "claude-opus-5")
    assert CONFIG.provider_model == "claude-opus-5"


def test_provider_model_override_wins(monkeypatch):
    monkeypatch.setattr(CONFIG, "api_provider", "openai")
    monkeypatch.setattr(CONFIG, "_provider_model_raw", "gpt-5-mini")
    assert CONFIG.provider_model == "gpt-5-mini"


def test_effective_brain_rules(monkeypatch):
    monkeypatch.setattr(CONFIG, "brain", "api")
    assert config_mod.effective_brain() == "api"
    monkeypatch.setattr(CONFIG, "brain", "subscription")
    assert config_mod.effective_brain() == "subscription"
    monkeypatch.setattr(CONFIG, "brain", "auto")
    monkeypatch.setattr(CONFIG, "api_provider", "anthropic")
    monkeypatch.setattr(CONFIG, "anthropic_key", "sk-test")
    assert config_mod.effective_brain() == "api"
    monkeypatch.setattr(CONFIG, "anthropic_key", "")
    assert config_mod.effective_brain() == "subscription"


def test_vision_warning_for_deepseek(monkeypatch):
    monkeypatch.setattr(CONFIG, "brain", "api")
    monkeypatch.setattr(CONFIG, "api_provider", "deepseek")
    monkeypatch.setattr(CONFIG, "_provider_model_raw", "")
    monkeypatch.setattr(CONFIG, "custom_model", "")
    warnings = config_mod.validate_warnings()
    assert len(warnings) == 1
    assert "cannot see images" in warnings[0]


def test_no_vision_warning_for_anthropic(monkeypatch):
    monkeypatch.setattr(CONFIG, "brain", "api")
    monkeypatch.setattr(CONFIG, "api_provider", "anthropic")
    assert config_mod.validate_warnings() == []


def test_no_vision_warning_in_subscription_mode(monkeypatch):
    monkeypatch.setattr(CONFIG, "brain", "subscription")
    monkeypatch.setattr(CONFIG, "api_provider", "deepseek")
    assert config_mod.validate_warnings() == []
