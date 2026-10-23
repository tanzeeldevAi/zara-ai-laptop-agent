import os
from pathlib import Path

from dotenv import load_dotenv

from .secrets import get_secret

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _int(name, default):
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def _float(name, default):
    try:
        return float(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def _ids(raw):
    out = set()
    for part in (raw or "").replace(";", ",").split(","):
        part = part.strip()
        if part.isdigit():
            out.add(int(part))
    return out


def _bool(name, default="false"):
    return os.getenv(name, default).strip().lower() in ("true", "1", "yes")


KNOWN_TRANSPORTS = ("discord", "telegram", "whatsapp")


def _transports(raw):
    """Parse TRANSPORTS (comma list). Blank/unset = discord only, so an
    existing install behaves exactly as before Phase 1. Unknown names are
    dropped; at least one known transport always remains."""
    out = []
    for part in (raw or "").replace(";", ",").split(","):
        name = part.strip().lower()
        if name in KNOWN_TRANSPORTS and name not in out:
            out.append(name)
    return out or ["discord"]


def _numbers(raw):
    """Parse a comma list of phone numbers into digit-only strings."""
    out = set()
    for part in (raw or "").replace(";", ",").split(","):
        digits = "".join(ch for ch in part if ch.isdigit())
        if digits:
            out.add(digits)
    return out


class Config:
    # ---- product identity (Phase 0: branding lives in config, not in code) ----
    product_name = os.getenv("PRODUCT_NAME", "CodeHubb Assistant").strip() or "CodeHubb Assistant"
    # The assistant's persona name. ASSISTANT_NAME wins; AGENT_NAME is the
    # original variable and keeps working. Buyers can name their assistant.
    assistant_name = (
        os.getenv("ASSISTANT_NAME", "").strip()
        or os.getenv("AGENT_NAME", "").strip()
        or "Zara"
    )
    agent_name = assistant_name  # legacy alias, used across the codebase

    # ---- secrets (OS keyring first, .env fallback — see secrets.py) ----
    discord_token = get_secret("DISCORD_BOT_TOKEN")
    anthropic_key = get_secret("ANTHROPIC_API_KEY")
    groq_api_key = get_secret("GROQ_API_KEY")
    elevenlabs_key = get_secret("ELEVENLABS_API_KEY")

    allowed_users = _ids(os.getenv("DISCORD_ALLOWED_USER_IDS"))

    # ---- transports (Phase 1: three optional front ends, one brain) ----
    # TRANSPORTS is a comma list: discord, telegram, whatsapp. Default keeps
    # an existing install Discord-only, exactly as before.
    transports = _transports(os.getenv("TRANSPORTS", "discord"))

    telegram_token = get_secret("TELEGRAM_BOT_TOKEN")
    telegram_allowed_users = _ids(os.getenv("TELEGRAM_ALLOWED_USER_IDS"))

    # WhatsApp (Route A, Baileys sidecar): no token — pairing is by QR scan.
    # The allowlist is phone numbers in international format, digits only.
    whatsapp_allowed_numbers = _numbers(os.getenv("WHATSAPP_ALLOWED_NUMBERS"))
    wa_bridge_port = _int("WA_BRIDGE_PORT", 17331)

    model = os.getenv("AGENT_MODEL", "claude-opus-5").strip() or "claude-opus-5"

    # Which brain to use:
    #   auto         -> subscription if no API key is set, else API
    #   subscription -> always use your Claude Code login (no per-token API cost)
    #   api          -> always use an API key (provider chosen by API_PROVIDER)
    brain = os.getenv("BRAIN", "auto").strip().lower() or "auto"
    # Model for subscription mode. 'opus' = best quality. Blank = your Claude Code default.
    subscription_model = os.getenv("SUBSCRIPTION_MODEL", "opus").strip()

    # ---- API provider (multi-provider brain, Phase 0) ----
    # anthropic | openai | gemini | deepseek | custom (any OpenAI-compatible
    # endpoint: OpenRouter, Groq, Mistral, xAI, Together, local Ollama...).
    api_provider = os.getenv("API_PROVIDER", "anthropic").strip().lower() or "anthropic"
    _provider_model_raw = os.getenv("PROVIDER_MODEL", "").strip()
    custom_api_base = os.getenv("CUSTOM_API_BASE", "").strip()
    custom_model = os.getenv("CUSTOM_MODEL", "").strip()

    @property
    def provider_model(self):
        """The model name for the API brain, in the provider's own naming."""
        if self._provider_model_raw:
            return self._provider_model_raw
        if self.api_provider == "anthropic":
            return self.model  # AGENT_MODEL keeps working exactly as before
        if self.api_provider == "custom":
            return self.custom_model
        from .brain.providers import get_provider

        provider = get_provider(self.api_provider)
        return provider.default_model if provider else ""

    def provider_key(self):
        """API key for the configured provider (keyring first, then .env)."""
        if self.api_provider == "anthropic":
            return self.anthropic_key
        from .brain.providers import get_provider

        provider = get_provider(self.api_provider)
        if provider is None or not provider.env_key:
            return ""
        if provider.env_key == "ANTHROPIC_API_KEY":
            return self.anthropic_key
        return get_secret(provider.env_key)

    # Long jobs (e.g. "generate images for 5 products") need hundreds of steps - every
    # click, wait and screenshot is one step. 30 was far too low and caused silent stalls.
    max_iterations = _int("MAX_ITERATIONS", 400)
    # If the step limit is still hit, carry on automatically this many times instead of
    # stopping dead while the owner is away.
    auto_continue = _int("AUTO_CONTINUE", 3)
    # Post a "gear" line to Discord for every single tool call. Noisy and slow - off by default.
    show_tool_calls = _bool("SHOW_TOOL_CALLS")
    shell_timeout = _int("SHELL_TIMEOUT", 180)
    history_turns = _int("HISTORY_TURNS", 30)
    confirm_risky = _bool("CONFIRM_RISKY")

    # ---- voice ----
    # voice replies: auto (mirror the user), always, or never
    voice_replies = os.getenv("VOICE_REPLIES", "auto").strip().lower()
    # faster-whisper model: tiny / base / small / medium. bigger = better Urdu, slower.
    # 'base' measured fastest-with-best-Urdu on this CPU: 3.7x quicker than 'small'
    # AND it returns proper Urdu script where 'small' returned Devanagari.
    whisper_model = os.getenv("WHISPER_MODEL", "base").strip() or "base"

    # Groq cloud speech-to-text: ~1-2s instead of ~13s locally, and whisper-large-v3 is
    # far more accurate (especially Urdu) than anything this CPU can run.
    # Falls back to the local model automatically if the key is missing or the call fails.
    groq_stt_model = os.getenv("GROQ_STT_MODEL", "whisper-large-v3").strip() or "whisper-large-v3"
    groq_timeout = _int("GROQ_TIMEOUT", 25)
    # Ava = one of the most natural/human female voices Microsoft ships. Uzma = best Urdu female.
    voice_english = os.getenv("VOICE_ENGLISH", "en-US-AvaMultilingualNeural").strip() or "en-US-AvaMultilingualNeural"
    voice_urdu = os.getenv("VOICE_URDU", "ur-PK-UzmaNeural").strip() or "ur-PK-UzmaNeural"

    # ---- realistic voice (ElevenLabs) ----
    # auto = use ElevenLabs when a key is set, otherwise the free edge-tts voices.
    tts_provider = os.getenv("TTS_PROVIDER", "auto").strip().lower() or "auto"
    # eleven_v3 is the only model that speaks Urdu AND understands emotion tags like [sighs].
    elevenlabs_model = os.getenv("ELEVENLABS_MODEL", "eleven_v3").strip() or "eleven_v3"
    elevenlabs_voice_en = os.getenv("ELEVENLABS_VOICE_EN", "9BWtsMINqrJLrRacOk9x").strip()
    elevenlabs_voice_ur = os.getenv("ELEVENLABS_VOICE_UR", "").strip()   # blank = same voice as English
    # lower stability = more emotional swing; 0.5 natural, 0.3 very expressive
    elevenlabs_stability = _float("ELEVENLABS_STABILITY", 0.45)
    elevenlabs_similarity = _float("ELEVENLABS_SIMILARITY", 0.75)
    tts_max_chars = _int("TTS_MAX_CHARS", 1400)
    # Slight speed-up + gentle pitch lift reads as more natural/conversational than the flat default.
    voice_rate = os.getenv("VOICE_RATE", "+8%").strip() or "+0%"
    voice_pitch = os.getenv("VOICE_PITCH", "+2Hz").strip() or "+0Hz"

    # ---- licensing (Phase 4) ----
    # The seller's Ed25519 PUBLIC key (base64 or PEM). This is a build-time
    # switch, set in the release build's .env: when it is EMPTY the build is
    # a development build and licensing is disabled entirely (unrestricted).
    # When set, trial/activation enforcement turns on. It is a public key —
    # not a secret — so it lives in plain config, never the keyring.
    license_public_key = os.getenv("LICENSE_PUBLIC_KEY", "").strip()
    # Optional activation/validation server (license_server/). Blank =
    # fully offline licensing: signed keys + grace rules only.
    license_server_url = os.getenv("LICENSE_SERVER_URL", "").strip().rstrip("/")

    # ---- browser tool (Phase 3, Playwright) ----
    # Headless by default; set BROWSER_HEADLESS=false to watch it work.
    browser_headless = _bool("BROWSER_HEADLESS", "true")

    max_tool_output = 6000          # chars of tool output handed back to the model
    discord_msg_limit = 1900        # Discord hard limit is 2000
    runtime_dir = ROOT / ".runtime"
    shots_dir = ROOT / ".runtime" / "shots"
    voice_dir = ROOT / ".runtime" / "voice"


CONFIG = Config()
CONFIG.runtime_dir.mkdir(parents=True, exist_ok=True)
CONFIG.shots_dir.mkdir(parents=True, exist_ok=True)
CONFIG.voice_dir.mkdir(parents=True, exist_ok=True)


def effective_brain():
    """Return 'subscription' or 'api' based on config and what's available."""
    if CONFIG.brain == "api":
        return "api"
    if CONFIG.brain == "subscription":
        return "subscription"
    # auto: API when the configured provider has a key (for anthropic that is
    # exactly the original rule), otherwise the Claude Code login.
    if CONFIG.api_provider == "custom":
        return "api" if CONFIG.custom_api_base else "subscription"
    return "api" if CONFIG.provider_key() else "subscription"


def _claude_cli_present():
    import shutil

    if shutil.which("claude"):
        return True
    # global npm install location on Windows
    candidate = Path(os.environ.get("APPDATA", "")) / "npm" / "claude.cmd"
    return candidate.exists()


def validate():
    """Return a list of human-readable setup problems (empty list = good to go)."""
    problems = []
    enabled = CONFIG.transports
    if "discord" in enabled:
        if not CONFIG.discord_token:
            problems.append("DISCORD_BOT_TOKEN is empty in .env")
        if not CONFIG.allowed_users:
            problems.append(
                "DISCORD_ALLOWED_USER_IDS is empty in .env - without it the agent "
                "would refuse everyone (or, worse, obey anyone). Put your own Discord user ID there."
            )
    if "telegram" in enabled:
        if not CONFIG.telegram_token:
            problems.append(
                "TELEGRAM_BOT_TOKEN is empty in .env - create a bot with @BotFather "
                "on Telegram and paste its token there, or remove telegram from TRANSPORTS."
            )
        if not CONFIG.telegram_allowed_users:
            problems.append(
                "TELEGRAM_ALLOWED_USER_IDS is empty in .env - without it the agent "
                "would refuse everyone (or, worse, obey anyone). Put your own Telegram user ID there "
                "(message @userinfobot to learn it)."
            )
    if "whatsapp" in enabled:
        if not CONFIG.whatsapp_allowed_numbers:
            problems.append(
                "WHATSAPP_ALLOWED_NUMBERS is empty in .env - without it the agent "
                "would refuse everyone (or, worse, obey anyone). Put your own number "
                "in international format, digits only (e.g. 923001234567)."
            )

    mode = effective_brain()
    if mode == "api":
        from .brain.providers import get_provider

        provider = get_provider(CONFIG.api_provider)
        if provider is None:
            problems.append(
                f"API_PROVIDER '{CONFIG.api_provider}' is not a known provider. "
                "Use anthropic, openai, gemini, deepseek or custom."
            )
        elif provider.id == "custom":
            if not CONFIG.custom_api_base:
                problems.append(
                    "API_PROVIDER=custom but CUSTOM_API_BASE is empty. Set it to your "
                    "OpenAI-compatible endpoint, e.g. https://openrouter.ai/api/v1 "
                    "or http://localhost:11434/v1 for a local Ollama server."
                )
            if not CONFIG.provider_model:
                problems.append(
                    "API_PROVIDER=custom but no model is set. Set CUSTOM_MODEL (or PROVIDER_MODEL) "
                    "to the model name your endpoint serves."
                )
        elif not CONFIG.provider_key():
            if provider.id == "anthropic":
                problems.append(
                    "BRAIN=api but ANTHROPIC_API_KEY is empty. Either add the key, or set BRAIN=subscription "
                    "to run on your Claude Code login instead."
                )
            else:
                problems.append(
                    f"BRAIN=api with API_PROVIDER={provider.id} but {provider.env_key} is empty. "
                    f"Add the key, or set BRAIN=subscription to run on your Claude Code login instead."
                )
    if mode == "subscription" and not _claude_cli_present():
        problems.append(
            "Subscription mode needs the Claude Code CLI, which wasn't found. Install it with "
            "`npm i -g @anthropic-ai/claude-code` and run `claude` once to sign in - or add an "
            "ANTHROPIC_API_KEY to .env to use the API instead."
        )
    return problems


def validate_warnings():
    """Non-blocking warnings (printed, but the agent still starts)."""
    warnings = []
    if effective_brain() == "api":
        from .brain.providers import get_provider, model_supports_vision

        provider = get_provider(CONFIG.api_provider)
        if provider is not None and not model_supports_vision(provider, CONFIG.provider_model):
            warnings.append(
                f"Model '{CONFIG.provider_model}' ({provider.display_name}) cannot see images. "
                "Screenshots will be sent to you, but the assistant will decide from text output only. "
                "For full screen control pick a vision model (Claude, GPT or Gemini families)."
            )
    return warnings
