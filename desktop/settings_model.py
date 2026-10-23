"""Qt-free settings model for the desktop GUI (Phase 2).

One Settings dataclass mirrors everything the setup wizard and the settings
window can edit. It loads from the .env file (via agent.envfile) and saves
back through it — with one hard rule from the plan:

    SECRET VALUES NEVER GO TO .env FROM THE GUI.

On save, each secret is stored through agent.secrets (OS keyring). The .env
line for that key is written EMPTY as a placeholder (the config loader reads
keyring first anyway). If the keyring is unavailable, the secret is NOT
written to .env and any value already sitting in .env is left untouched —
the GUI reports a warning instead of silently downgrading security or
silently losing the user's token.

Brain-mode / provider semantics mirror agent.config exactly:
  - anthropic's model lives in AGENT_MODEL, custom's in CUSTOM_MODEL,
    other providers use PROVIDER_MODEL; subscription uses BRAIN/SUBSCRIPTION_MODEL.
The secret getters/setters are injectable so tests never touch a real keyring.
"""

from dataclasses import dataclass, field
from pathlib import Path

from agent import config as agent_config
from agent import secrets as agent_secrets
from agent.brain.providers import PROVIDERS, get_provider, model_supports_vision
from agent.envfile import EnvFile

ROOT = Path(__file__).resolve().parent.parent
SECRET_FIELDS = {
    # settings attribute -> env var name (must stay in sync with secrets.SECRET_KEYS)
    "discord_token": "DISCORD_BOT_TOKEN",
    "telegram_token": "TELEGRAM_BOT_TOKEN",
    "anthropic_key": "ANTHROPIC_API_KEY",
    "openai_key": "OPENAI_API_KEY",
    "gemini_key": "GEMINI_API_KEY",
    "deepseek_key": "DEEPSEEK_API_KEY",
    "custom_key": "CUSTOM_API_KEY",
    "groq_key": "GROQ_API_KEY",
    "elevenlabs_key": "ELEVENLABS_API_KEY",
}

BRAIN_CHOICES = ("auto", "subscription", "api")
VOICE_CHOICES = ("auto", "always", "never")


@dataclass
class SaveResult:
    warnings: list = field(default_factory=list)
    keyring_ok: bool = True


@dataclass
class Settings:
    product_name: str = "CodeHubb Assistant"
    assistant_name: str = "Zara"
    brain: str = "auto"                       # auto | subscription | api
    api_provider: str = "anthropic"
    provider_model: str = ""                  # provider-naming model (see module docstring)
    subscription_model: str = "opus"
    custom_api_base: str = ""
    transports: list = field(default_factory=lambda: ["discord"])
    discord_allowed_user_ids: str = ""
    telegram_allowed_user_ids: str = ""
    whatsapp_allowed_numbers: str = ""
    voice_replies: str = "auto"
    confirm_risky: bool = False
    show_tool_calls: bool = False
    max_iterations: int = 400
    # secrets (transient; persisted only to the keyring)
    discord_token: str = ""
    telegram_token: str = ""
    anthropic_key: str = ""
    openai_key: str = ""
    gemini_key: str = ""
    deepseek_key: str = ""
    custom_key: str = ""
    groq_key: str = ""
    elevenlabs_key: str = ""
    # bookkeeping
    fresh_install: bool = False               # .env was bootstrapped just now

    # ------------------------------------------------------------- loading

    @classmethod
    def load(cls, env_path=None, *, get_secret_fn=None):
        """Load settings from the .env file (+ keyring for secrets).

        A missing .env is bootstrapped from .env.example and flagged as a
        fresh install (the wizard then defaults CONFIRM_RISKY to ON, per the
        distributed-build policy in the plan)."""
        env_path = Path(env_path) if env_path else ROOT / ".env"
        env, created = EnvFile.bootstrap(env_path, ROOT / ".env.example")
        get_secret_fn = get_secret_fn or agent_secrets.get_secret

        def val(key, default=""):
            found = env.get(key)
            return default if found is None else found

        provider = (val("API_PROVIDER", "anthropic").strip().lower() or "anthropic")
        if provider == "anthropic":
            provider_model = val("AGENT_MODEL", "claude-opus-5")
        elif provider == "custom":
            provider_model = val("PROVIDER_MODEL") or val("CUSTOM_MODEL")
        else:
            provider_model = val("PROVIDER_MODEL")
            if not provider_model:
                known = get_provider(provider)
                provider_model = known.default_model if known else ""

        transports = agent_config._transports(val("TRANSPORTS", "discord"))
        try:
            max_iterations = int(val("MAX_ITERATIONS", "400") or 400)
        except ValueError:
            max_iterations = 400

        confirm_risky = _parse_bool(val("CONFIRM_RISKY"), False)
        if created:
            # Fresh distributed install: confirmations default ON (plan
            # policy), whatever the example file says — the user can turn
            # them off in the wizard, deliberately.
            confirm_risky = True

        settings = cls(
            product_name=val("PRODUCT_NAME", "CodeHubb Assistant").strip() or "CodeHubb Assistant",
            assistant_name=(
                val("ASSISTANT_NAME").strip() or val("AGENT_NAME").strip() or "Zara"
            ),
            brain=(val("BRAIN", "auto").strip().lower() or "auto"),
            api_provider=provider,
            provider_model=provider_model,
            subscription_model=val("SUBSCRIPTION_MODEL", "opus"),
            custom_api_base=val("CUSTOM_API_BASE"),
            transports=transports,
            discord_allowed_user_ids=val("DISCORD_ALLOWED_USER_IDS"),
            telegram_allowed_user_ids=val("TELEGRAM_ALLOWED_USER_IDS"),
            whatsapp_allowed_numbers=val("WHATSAPP_ALLOWED_NUMBERS"),
            voice_replies=(val("VOICE_REPLIES", "auto").strip().lower() or "auto"),
            confirm_risky=confirm_risky,
            show_tool_calls=_parse_bool(val("SHOW_TOOL_CALLS"), False),
            max_iterations=max_iterations,
            fresh_install=created,
        )
        for attr, env_name in SECRET_FIELDS.items():
            setattr(settings, attr, get_secret_fn(env_name) or "")
        return settings

    # -------------------------------------------------------------- saving

    def save(self, env_path=None, *, set_secret_fn=None, delete_secret_fn=None):
        """Write settings: non-secrets to .env, secrets to the keyring only.

        Returns a SaveResult; warnings explain anything that could not be
        stored in the keyring (the affected .env lines are left as they were,
        so nothing is lost and nothing is downgraded to plaintext)."""
        env_path = Path(env_path) if env_path else ROOT / ".env"
        env, _created = EnvFile.bootstrap(env_path, ROOT / ".env.example")
        set_secret_fn = set_secret_fn or agent_secrets.set_secret
        delete_secret_fn = delete_secret_fn or agent_secrets.delete_secret
        result = SaveResult()

        env.set("PRODUCT_NAME", self.product_name)
        env.set("ASSISTANT_NAME", self.assistant_name)
        env.set("BRAIN", self.brain)
        env.set("API_PROVIDER", self.api_provider)
        # Model placement mirrors agent.config.provider_model semantics.
        if self.api_provider == "anthropic":
            env.set("AGENT_MODEL", self.provider_model or "claude-opus-5")
            env.set("PROVIDER_MODEL", "")
        elif self.api_provider == "custom":
            env.set("CUSTOM_MODEL", self.provider_model)
            env.set("PROVIDER_MODEL", "")
        else:
            env.set("PROVIDER_MODEL", self.provider_model)
        env.set("SUBSCRIPTION_MODEL", self.subscription_model)
        env.set("CUSTOM_API_BASE", self.custom_api_base)
        env.set("TRANSPORTS", ",".join(self.transports) or "discord")
        env.set("DISCORD_ALLOWED_USER_IDS", self.discord_allowed_user_ids)
        env.set("TELEGRAM_ALLOWED_USER_IDS", self.telegram_allowed_user_ids)
        env.set("WHATSAPP_ALLOWED_NUMBERS", self.whatsapp_allowed_numbers)
        env.set("VOICE_REPLIES", self.voice_replies)
        env.set("CONFIRM_RISKY", "true" if self.confirm_risky else "false")
        env.set("SHOW_TOOL_CALLS", "true" if self.show_tool_calls else "false")
        env.set("MAX_ITERATIONS", str(int(self.max_iterations)))

        for attr, env_name in SECRET_FIELDS.items():
            value = (getattr(self, attr) or "").strip()
            if value:
                if set_secret_fn(env_name, value):
                    env.set(env_name, "")   # placeholder: the real value is in the keyring
                else:
                    result.keyring_ok = False
                    result.warnings.append(
                        f"{env_name}: the OS keyring is unavailable, so this secret was "
                        "NOT written to .env (the GUI never stores secrets in plaintext). "
                        "Any value already in .env was left untouched."
                    )
            else:
                delete_secret_fn(env_name)
                env.set(env_name, "")

        env.save()
        return result

    # ---------------------------------------------------------- validation

    def validate(self):
        """GUI-side validation mirroring agent.config.validate()'s rules,
        evaluated against these settings (not the process-global CONFIG)."""
        problems = []
        if "discord" in self.transports:
            if not self.discord_token:
                problems.append("Discord is enabled but no bot token is set.")
            if not agent_config._ids(self.discord_allowed_user_ids):
                problems.append("Discord is enabled but no allowed user ID is set — nobody would be able to command the agent.")
        if "telegram" in self.transports:
            if not self.telegram_token:
                problems.append("Telegram is enabled but no bot token is set (create one with @BotFather).")
            if not agent_config._ids(self.telegram_allowed_user_ids):
                problems.append("Telegram is enabled but no allowed user ID is set — nobody would be able to command the agent.")
        if "whatsapp" in self.transports:
            if not agent_config._numbers(self.whatsapp_allowed_numbers):
                problems.append("WhatsApp is enabled but no allowed number is set — nobody would be able to command the agent.")

        mode = self._effective_brain()
        if mode == "api":
            provider = get_provider(self.api_provider)
            if provider is None:
                problems.append(
                    f"Unknown API provider '{self.api_provider}'. Use one of: "
                    + ", ".join(sorted(PROVIDERS))
                )
            elif provider.id == "custom":
                if not self.custom_api_base:
                    problems.append("Custom provider selected but no base URL is set.")
                if not self.provider_model:
                    problems.append("Custom provider selected but no model name is set.")
            elif not self._provider_key():
                problems.append(f"{provider.display_name} selected but its API key is empty.")
        if mode == "subscription" and not agent_config._claude_cli_present():
            problems.append(
                "Subscription mode needs the Claude Code CLI, which wasn't found on this machine."
            )
        return problems

    def vision_warning(self):
        """Plain-language warning when the chosen API model can't see images."""
        if self._effective_brain() != "api":
            return ""
        provider = get_provider(self.api_provider)
        if provider is None or model_supports_vision(provider, self.provider_model):
            return ""
        return (
            f"Heads up: {provider.display_name} model '{self.provider_model}' cannot see images. "
            "Screenshots will still be sent to you, but the assistant will decide from text only."
        )

    def _provider_key(self):
        if self.api_provider == "anthropic":
            return self.anthropic_key
        provider = get_provider(self.api_provider)
        if provider is None:
            return ""
        attr_by_env = {env: attr for attr, env in SECRET_FIELDS.items()}
        attr = attr_by_env.get(provider.env_key)
        return getattr(self, attr, "") if attr else ""

    def _effective_brain(self):
        if self.brain == "api":
            return "api"
        if self.brain == "subscription":
            return "subscription"
        if self.api_provider == "custom":
            return "api" if self.custom_api_base else "subscription"
        return "api" if self._provider_key() else "subscription"


def _parse_bool(raw, default=False):
    if raw is None:
        return default
    return str(raw).strip().lower() in ("true", "1", "yes")
