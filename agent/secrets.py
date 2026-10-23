"""Secret storage: OS keyring first, .env / environment as fallback.

Phase 0 of the CodeHubb Assistant rebuild moves API keys and tokens out of the
plaintext .env file where the platform offers something better: the OS keyring
(Windows Credential Manager, macOS Keychain, Linux Secret Service).

Lookup order for every covered secret:
    1. OS keyring, service "codehubb-assistant", username = the env var name.
    2. The process environment / .env value (the long-standing setup path).

A machine without a working keyring backend (headless Linux, locked keychain,
CI) must never crash the agent: every keyring call is wrapped, failures are
logged once, and the .env fallback quietly takes over.
"""

import os
import sys

SERVICE_NAME = "codehubb-assistant"

# Env var names whose values are secrets worth keeping in the keyring.
SECRET_KEYS = (
    "DISCORD_BOT_TOKEN",
    "TELEGRAM_BOT_TOKEN",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "DEEPSEEK_API_KEY",
    "CUSTOM_API_KEY",
    "GROQ_API_KEY",
    "ELEVENLABS_API_KEY",
    # Phase 4: the activated license key + the server's validation token.
    # A license key is a bearer credential — keyring only, never .env/SQLite
    # (the .env fallback in get_secret exists so a keyring-less machine can
    # still be activated by hand; see agent/licensing.py).
    "LICENSE_KEY",
    "LICENSE_TOKEN",
)

_warned = False


def _keyring():
    """Return the keyring module, or None if unavailable. Never raises."""
    global _warned
    try:
        import keyring

        # Force a cheap probe: some backends import fine but fail on first use.
        keyring.get_keyring()
        return keyring
    except Exception as exc:  # noqa: BLE001 - keyring must never break startup
        if not _warned:
            _warned = True
            print(f"[secrets] keyring unavailable ({exc}); using .env values.", file=sys.stderr)
        return None


def get_secret(name, default=""):
    """Keyring first, then environment/.env. Returns `default` when unset."""
    kr = _keyring()
    if kr is not None:
        try:
            value = kr.get_password(SERVICE_NAME, name)
        except Exception as exc:  # noqa: BLE001
            global _warned
            if not _warned:
                _warned = True
                print(f"[secrets] keyring read failed ({exc}); using .env values.", file=sys.stderr)
            value = None
        if value:
            return value.strip()
    return (os.getenv(name, "") or default).strip()


def set_secret(name, value):
    """Store a secret in the OS keyring. Returns True on success."""
    kr = _keyring()
    if kr is None:
        return False
    try:
        kr.set_password(SERVICE_NAME, name, value)
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[secrets] could not store {name} in keyring: {exc}", file=sys.stderr)
        return False


def delete_secret(name):
    """Remove a secret from the OS keyring. Returns True on success."""
    kr = _keyring()
    if kr is None:
        return False
    try:
        kr.delete_password(SERVICE_NAME, name)
        return True
    except Exception:  # noqa: BLE001
        return False
