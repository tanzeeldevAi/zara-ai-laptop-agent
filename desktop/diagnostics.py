"""Diagnostics bundle (Qt-free): the "report a problem" text.

One plain-text report the user can copy or save and send to support:
versions, what config validation complains about, whether each transport
is configured (booleans only — a secret VALUE never appears here), whether
Node/the sidecar/keyring are available, and the tails of the audit log and
the agent shell log. Every fact-gatherer is injectable for tests.
"""

import platform
import shutil
import sys
from pathlib import Path

from agent import __version__ as agent_version
from agent import config as agent_config
from agent import secrets as agent_secrets

ROOT = Path(__file__).resolve().parent.parent


def transport_state(config=agent_config.CONFIG):
    """(name, enabled, configured, note) per known transport."""
    rows = []
    enabled = set(config.transports)
    discord_ok = bool(config.discord_token) and bool(config.allowed_users)
    rows.append(("discord", "discord" in enabled, discord_ok,
                 "token + allowlist set" if discord_ok else "token or allowlist missing"))
    telegram_ok = bool(config.telegram_token) and bool(config.telegram_allowed_users)
    rows.append(("telegram", "telegram" in enabled, telegram_ok,
                 "token + allowlist set" if telegram_ok else "token or allowlist missing"))
    wa_ok = bool(config.whatsapp_allowed_numbers)
    rows.append(("whatsapp", "whatsapp" in enabled, wa_ok,
                 "allowlist set (pairing is separate)" if wa_ok else "allowlist missing"))
    return rows


def _tail(path, count):
    path = Path(path)
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8", errors="replace").splitlines()[-count:]


def build_report(
    root=None,
    *,
    problems=None,
    warnings=None,
    transports=None,
    node_present=None,
    sidecar_ready=None,
    keyring_ok=None,
    audit_lines=None,
    shell_log_lines=None,
    license_info=None,
):
    root = Path(root) if root else ROOT
    config = agent_config.CONFIG
    if problems is None:
        problems = agent_config.validate()
    if warnings is None:
        warnings = agent_config.validate_warnings()
    if transports is None:
        transports = transport_state(config)
    if node_present is None:
        node_present = shutil.which("node") is not None
    if sidecar_ready is None:
        sidecar_ready = (root / "sidecar" / "node_modules").exists()
    if keyring_ok is None:
        keyring_ok = agent_secrets._keyring() is not None
    runtime = root / ".runtime"
    if audit_lines is None:
        audit_lines = _tail(runtime / "audit.jsonl", 20)
    if shell_log_lines is None:
        shell_log_lines = _tail(runtime / "agent-shell.log", 40)

    out = []
    out.append(f"{config.product_name} — diagnostics")
    out.append("=" * 60)
    out.append(f"Product version : {agent_version}")
    out.append(f"Python          : {sys.version.split()[0]} ({platform.system()} {platform.release()}, {platform.machine()})")
    out.append(f"Assistant name  : {config.assistant_name}")
    out.append(f"Brain mode      : {config.brain} (effective: {agent_config.effective_brain()})")
    if agent_config.effective_brain() == "api":
        out.append(f"API provider    : {config.api_provider} / model {config.provider_model}")
    out.append(f"Transports      : {', '.join(config.transports)}")
    out.append("")
    out.append("Setup problems (must be empty for the agent to start):")
    if problems:
        out.extend(f"  - {p}" for p in problems)
    else:
        out.append("  (none)")
    if warnings:
        out.append("Warnings:")
        out.extend(f"  - {w}" for w in warnings)
    out.append("")
    out.append("Transports detail:")
    for name, enabled, configured, note in transports:
        out.append(
            f"  {name:<9} enabled={'yes' if enabled else 'no ':<3} configured={'yes' if configured else 'no'} — {note}"
        )
    out.append("")
    out.append("Environment:")
    out.append(f"  Node.js present        : {'yes' if node_present else 'no'}")
    out.append(f"  WhatsApp bridge ready  : {'yes (node_modules installed)' if sidecar_ready else 'no (npm install not run in sidecar/)'}")
    out.append(f"  OS keyring available   : {'yes' if keyring_ok else 'no — secrets fall back to .env'}")
    out.append("")
    out.append("License:")
    if license_info is None:
        try:
            from agent import licensing as _licensing

            license_info = _licensing.get_manager().status()
        except Exception:  # noqa: BLE001 - diagnostics must always render
            license_info = None
    if license_info is None:
        out.append("  (status unavailable)")
    else:
        # State only — the license KEY itself never appears in diagnostics.
        out.append(f"  State          : {license_info.state}")
        if license_info.plan:
            out.append(f"  Plan           : {license_info.plan}")
        if license_info.buyer_email:
            out.append(f"  Licensed to    : {license_info.buyer_email}")
        if license_info.days_left is not None:
            out.append(f"  Days left      : {license_info.days_left}")
        if license_info.machines_limit is not None:
            used = license_info.machines_used
            out.append(
                f"  Machines       : {used if used is not None else '?'} of "
                f"{license_info.machines_limit} used"
            )
    out.append("")
    out.append("Last audit entries (up to 20):")
    if audit_lines:
        out.extend(f"  {line}" for line in audit_lines)
    else:
        out.append("  (audit log is empty or missing)")
    out.append("")
    out.append("Agent shell log (last 40 lines):")
    if shell_log_lines:
        out.extend(f"  {line}" for line in shell_log_lines)
    else:
        out.append("  (no shell log yet — the agent has not been started from the app)")
    out.append("")
    return "\n".join(out)
