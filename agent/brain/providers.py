"""LLM provider registry for the API brain (LiteLLM-backed).

One registry entry per provider family, with the capability facts the rest of
the system needs — above all: can this model family *see* images (the whole
screenshot workflow depends on it) and can it call tools.

Providers:
    anthropic  Claude models, via ANTHROPIC_API_KEY
    openai     GPT models, via OPENAI_API_KEY
    gemini     Google Gemini models, via GEMINI_API_KEY
    deepseek   DeepSeek models, via DEEPSEEK_API_KEY (text-strong, no vision)
    custom     ANY OpenAI-compatible endpoint — the user supplies base URL +
               key + model name. One door covers OpenRouter, Groq, Mistral,
               xAI, Together, and local Ollama / LM Studio servers.

Capability flags are per model *family*: known model-name prefixes refine the
provider defaults (e.g. DeepSeek's reasoner vs chat). Anything unknown is
treated conservatively for vision (assumed absent) but optimistically for
tools, and the wizard/test call is the real check.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Provider:
    id: str
    display_name: str
    litellm_prefix: str          # "" means the model string is used as-is
    env_key: str                 # env var holding the API key ("" for custom)
    default_model: str
    supports_tools: bool = True
    supports_vision: bool = True
    notes: str = ""
    # (model-name substring, vision) refinements, first match wins
    vision_overrides: tuple = field(default_factory=tuple)


PROVIDERS = {
    "anthropic": Provider(
        id="anthropic",
        display_name="Anthropic (Claude)",
        litellm_prefix="anthropic",
        env_key="ANTHROPIC_API_KEY",
        default_model="claude-opus-5",
        supports_tools=True,
        supports_vision=True,
        notes="Full capability: tools + vision. Also available via subscription mode.",
    ),
    "openai": Provider(
        id="openai",
        display_name="OpenAI (GPT)",
        litellm_prefix="openai",
        env_key="OPENAI_API_KEY",
        default_model="gpt-5",
        supports_tools=True,
        supports_vision=True,
        notes="Full capability on the GPT-4o/GPT-5 families.",
        vision_overrides=(("gpt-4o", True), ("gpt-5", True), ("o3", True), ("o4", True)),
    ),
    "gemini": Provider(
        id="gemini",
        display_name="Google (Gemini)",
        litellm_prefix="gemini",
        env_key="GEMINI_API_KEY",
        default_model="gemini-2.5-pro",
        supports_tools=True,
        supports_vision=True,
        notes="Full capability: tools + vision on Gemini 2.x.",
    ),
    "deepseek": Provider(
        id="deepseek",
        display_name="DeepSeek",
        litellm_prefix="deepseek",
        env_key="DEEPSEEK_API_KEY",
        default_model="deepseek-chat",
        supports_tools=True,
        supports_vision=False,
        notes=(
            "Strong and inexpensive for text, but the API models do not see images: "
            "screenshots are sent to the owner, decisions are made from text output only."
        ),
    ),
    "custom": Provider(
        id="custom",
        display_name="Custom (OpenAI-compatible)",
        litellm_prefix="openai",
        env_key="CUSTOM_API_KEY",
        default_model="",
        supports_tools=True,
        supports_vision=False,   # unknown endpoint: assume no vision until proven
        notes=(
            "Any OpenAI-compatible endpoint: OpenRouter, Groq, Mistral, xAI, Together, "
            "or a local Ollama / LM Studio server. Vision depends on the chosen model."
        ),
    ),
}


def get_provider(provider_id):
    return PROVIDERS.get((provider_id or "anthropic").strip().lower())


def model_supports_vision(provider, model):
    """Best-known answer for this specific model name."""
    name = (model or "").lower()
    for needle, vision in provider.vision_overrides:
        if needle in name:
            return vision
    return provider.supports_vision


def build_model_string(provider, model):
    """The LiteLLM model string for a provider + bare model name.

    Already-prefixed names ("anthropic/...", "openai/...") pass through, so
    power users can write full LiteLLM strings in PROVIDER_MODEL.
    """
    model = (model or provider.default_model or "").strip()
    if not model:
        return ""
    if "/" in model and not provider.litellm_prefix == "":
        head = model.split("/", 1)[0]
        if head in _KNOWN_PREFIXES:
            return model
    if provider.litellm_prefix:
        return f"{provider.litellm_prefix}/{model}"
    return model


_KNOWN_PREFIXES = {
    "anthropic", "openai", "gemini", "deepseek", "groq", "mistral", "xai",
    "openrouter", "together_ai", "ollama", "azure", "bedrock", "vertex_ai",
}

NO_VISION_NOTICE = (
    "IMPORTANT - your current model cannot see images. When a tool returns a "
    "screenshot it has been sent to the owner, but you cannot view it yourself. "
    "Work from the tools' text output (window titles, file listings, command "
    "output) and say plainly when a step needs the owner's eyes."
)
