"""Settings model: load/save mapping, and the hard rule — secrets land in
the keyring, never in .env."""

import pytest

from desktop.settings_model import SECRET_FIELDS, Settings


class FakeKeyring:
    def __init__(self, available=True):
        self.store = {}
        self.available = available
        self.deleted = []

    def get(self, name):
        return self.store.get(name, "")

    def set(self, name, value):
        if not self.available:
            return False
        self.store[name] = value
        return True

    def delete(self, name):
        self.deleted.append(name)
        self.store.pop(name, None)
        return True


@pytest.fixture
def keyring():
    return FakeKeyring()


@pytest.fixture
def env_path(tmp_path):
    example = tmp_path / ".env.example"
    example.write_text("PRODUCT_NAME=CodeHubb Assistant\nCONFIRM_RISKY=false\n", encoding="utf-8")
    return tmp_path / ".env"


def _load(tmp_path, keyring, env_path):
    # settings_model resolves .env.example relative to the repo ROOT; redirect
    # by pointing env_path into tmp and monkeypatching the example location.
    import desktop.settings_model as sm

    original_root = sm.ROOT
    sm.ROOT = tmp_path
    try:
        return Settings.load(env_path, get_secret_fn=keyring.get)
    finally:
        sm.ROOT = original_root


def _save(settings, tmp_path, keyring, env_path):
    import desktop.settings_model as sm

    original_root = sm.ROOT
    sm.ROOT = tmp_path
    try:
        return settings.save(env_path, set_secret_fn=keyring.set, delete_secret_fn=keyring.delete)
    finally:
        sm.ROOT = original_root


def test_load_bootstraps_and_marks_fresh(tmp_path, keyring, env_path):
    settings = _load(tmp_path, keyring, env_path)
    assert settings.fresh_install is True
    assert env_path.exists()
    # fresh distributed install: confirmations default ON despite example=false
    assert settings.confirm_risky is True


def test_load_existing_values(tmp_path, keyring, env_path):
    env_path.write_text(
        "ASSISTANT_NAME=Ayesha\nBRAIN=api\nAPI_PROVIDER=openai\nPROVIDER_MODEL=gpt-5-mini\n"
        "TRANSPORTS=telegram,discord\nVOICE_REPLIES=always\nCONFIRM_RISKY=true\nMAX_ITERATIONS=77\n",
        encoding="utf-8",
    )
    settings = _load(tmp_path, keyring, env_path)
    assert settings.fresh_install is False
    assert settings.assistant_name == "Ayesha"
    assert settings.brain == "api"
    assert settings.api_provider == "openai"
    assert settings.provider_model == "gpt-5-mini"
    assert settings.transports == ["telegram", "discord"]
    assert settings.voice_replies == "always"
    assert settings.max_iterations == 77


def test_load_anthropic_model_comes_from_agent_model(tmp_path, keyring, env_path):
    env_path.write_text("API_PROVIDER=anthropic\nAGENT_MODEL=claude-sonnet-5\n", encoding="utf-8")
    settings = _load(tmp_path, keyring, env_path)
    assert settings.provider_model == "claude-sonnet-5"


def test_load_secrets_come_from_keyring(tmp_path, keyring, env_path):
    keyring.store["DISCORD_BOT_TOKEN"] = "tok-123"
    env_path.write_text("TRANSPORTS=discord\n", encoding="utf-8")
    settings = _load(tmp_path, keyring, env_path)
    assert settings.discord_token == "tok-123"


def test_save_writes_plain_values_and_keyring_secrets(tmp_path, keyring, env_path):
    settings = Settings(
        assistant_name="Noor",
        brain="api",
        api_provider="openai",
        provider_model="gpt-5",
        transports=["discord", "telegram"],
        discord_allowed_user_ids="111,222",
        telegram_allowed_user_ids="333",
        confirm_risky=True,
        discord_token="discord-secret",
        telegram_token="telegram-secret",
        openai_key="sk-openai",
    )
    result = _save(settings, tmp_path, keyring, env_path)
    assert result.warnings == []
    text = env_path.read_text(encoding="utf-8")
    assert "ASSISTANT_NAME=Noor" in text
    assert "TRANSPORTS=discord,telegram" in text
    assert "DISCORD_ALLOWED_USER_IDS=111,222" in text
    assert "CONFIRM_RISKY=true" in text
    assert "PROVIDER_MODEL=gpt-5" in text
    # secrets: in the keyring, and .env carries only empty placeholders
    assert keyring.store["DISCORD_BOT_TOKEN"] == "discord-secret"
    assert keyring.store["TELEGRAM_BOT_TOKEN"] == "telegram-secret"
    assert keyring.store["OPENAI_API_KEY"] == "sk-openai"
    assert "discord-secret" not in text
    assert "telegram-secret" not in text
    assert "sk-openai" not in text
    assert "DISCORD_BOT_TOKEN=\n" in text or text.rstrip().endswith("DISCORD_BOT_TOKEN=")


def test_save_anthropic_model_goes_to_agent_model(tmp_path, keyring, env_path):
    settings = Settings(api_provider="anthropic", provider_model="claude-opus-5")
    _save(settings, tmp_path, keyring, env_path)
    text = env_path.read_text(encoding="utf-8")
    assert "AGENT_MODEL=claude-opus-5" in text


def test_save_without_keyring_preserves_existing_env_secret(tmp_path, env_path):
    broken = FakeKeyring(available=False)
    env_path.write_text("DISCORD_BOT_TOKEN=old-token-in-env\n", encoding="utf-8")
    settings = Settings(discord_token="new-token")
    result = _save(settings, tmp_path, broken, env_path)
    text = env_path.read_text(encoding="utf-8")
    # not upgraded to keyring, but also NOT overwritten/lost and NOT duplicated
    assert "old-token-in-env" in text
    assert "new-token" not in text
    assert any("DISCORD_BOT_TOKEN" in w for w in result.warnings)
    assert result.keyring_ok is False


def test_save_cleared_secret_deletes_keyring_and_env(tmp_path, keyring, env_path):
    keyring.store["DISCORD_BOT_TOKEN"] = "tok"
    env_path.write_text("DISCORD_BOT_TOKEN=tok\n", encoding="utf-8")
    settings = Settings(discord_token="")
    _save(settings, tmp_path, keyring, env_path)
    assert "DISCORD_BOT_TOKEN" in keyring.deleted
    assert "tok" not in env_path.read_text(encoding="utf-8")


def test_validate_flags_enabled_transport_without_token():
    settings = Settings(transports=["telegram"], telegram_token="", telegram_allowed_user_ids="5")
    problems = settings.validate()
    assert any("Telegram" in p and "token" in p for p in problems)


def test_validate_flags_missing_allowlist():
    settings = Settings(transports=["whatsapp"], whatsapp_allowed_numbers="")
    problems = settings.validate()
    assert any("WhatsApp" in p for p in problems)


def test_validate_api_provider_without_key():
    settings = Settings(transports=[], brain="api", api_provider="deepseek", deepseek_key="")
    problems = settings.validate()
    assert any("DeepSeek" in p for p in problems)


def test_validate_custom_needs_base_and_model():
    settings = Settings(transports=[], brain="api", api_provider="custom",
                        custom_api_base="", provider_model="")
    problems = settings.validate()
    assert any("base URL" in p for p in problems)
    assert any("model" in p for p in problems)


def test_vision_warning_for_deepseek():
    settings = Settings(brain="api", api_provider="deepseek",
                        provider_model="deepseek-chat", deepseek_key="k")
    assert "cannot see images" in settings.vision_warning()


def test_vision_warning_empty_for_gemini():
    settings = Settings(brain="api", api_provider="gemini",
                        provider_model="gemini-2.5-pro", gemini_key="k")
    assert settings.vision_warning() == ""


def test_secret_fields_cover_all_keyring_keys():
    from agent.secrets import SECRET_KEYS

    # Phase 4: LICENSE_KEY / LICENSE_TOKEN are keyring secrets too, but
    # they are deliberately NOT Settings fields — the license panel /
    # agent.licensing manages them, and they must never flow through the
    # settings save path. Everything else must stay in exact sync.
    licensing_managed = {"LICENSE_KEY", "LICENSE_TOKEN"}
    assert set(SECRET_FIELDS.values()) == set(SECRET_KEYS) - licensing_managed
