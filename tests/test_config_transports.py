"""Phase 1: TRANSPORTS parsing and per-transport validation."""

from agent import config as config_mod

CONFIG = config_mod.CONFIG


def test_transports_parser_defaults_and_normalises():
    parse = config_mod._transports
    assert parse("") == ["discord"]
    assert parse(None) == ["discord"]
    assert parse("discord") == ["discord"]
    assert parse("telegram, whatsapp") == ["telegram", "whatsapp"]
    assert parse(" WhatsApp ; DISCORD ") == ["whatsapp", "discord"]
    assert parse("telegram,telegram") == ["telegram"]
    assert parse("bogus") == ["discord"]
    assert parse("telegram,bogus,discord") == ["telegram", "discord"]


def test_numbers_parser_digits_only():
    parse = config_mod._numbers
    assert parse("+92 300 1234567") == {"923001234567"}
    assert parse("923001234567, +1 (555) 010-2233") == {"923001234567", "15550102233"}
    assert parse("") == set()


def _base(monkeypatch, transports):
    """Valid brain + no discord requirements leaking into other transports."""
    monkeypatch.setattr(CONFIG, "transports", transports)
    monkeypatch.setattr(CONFIG, "brain", "subscription")
    monkeypatch.setattr(config_mod, "_claude_cli_present", lambda: True)


def test_telegram_missing_settings_flagged_discord_not(monkeypatch):
    _base(monkeypatch, ["telegram"])
    monkeypatch.setattr(CONFIG, "discord_token", "")       # discord off: must NOT be flagged
    monkeypatch.setattr(CONFIG, "allowed_users", set())
    monkeypatch.setattr(CONFIG, "telegram_token", "")
    monkeypatch.setattr(CONFIG, "telegram_allowed_users", set())
    problems = config_mod.validate()
    assert any("TELEGRAM_BOT_TOKEN" in p for p in problems)
    assert any("TELEGRAM_ALLOWED_USER_IDS" in p for p in problems)
    assert not any("DISCORD" in p for p in problems)


def test_telegram_configured_is_clean(monkeypatch):
    _base(monkeypatch, ["telegram"])
    monkeypatch.setattr(CONFIG, "telegram_token", "123:abc")
    monkeypatch.setattr(CONFIG, "telegram_allowed_users", {42})
    problems = config_mod.validate()
    assert not any("TELEGRAM" in p for p in problems)


def test_whatsapp_missing_numbers_flagged(monkeypatch):
    _base(monkeypatch, ["whatsapp"])
    monkeypatch.setattr(CONFIG, "whatsapp_allowed_numbers", set())
    problems = config_mod.validate()
    assert any("WHATSAPP_ALLOWED_NUMBERS" in p for p in problems)


def test_all_transports_configured_is_clean(monkeypatch):
    _base(monkeypatch, ["discord", "telegram", "whatsapp"])
    monkeypatch.setattr(CONFIG, "discord_token", "x")
    monkeypatch.setattr(CONFIG, "allowed_users", {1})
    monkeypatch.setattr(CONFIG, "telegram_token", "x")
    monkeypatch.setattr(CONFIG, "telegram_allowed_users", {2})
    monkeypatch.setattr(CONFIG, "whatsapp_allowed_numbers", {"923001234567"})
    assert config_mod.validate() == []
