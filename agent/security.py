"""Decides which actions pause and ask you before they run."""

import re

# Tools that always need a yes/no before they fire.
ALWAYS_CONFIRM = {
    "write_file",
    "delete_path",
    "kill_process",
    "registry_set",
}

# Shell commands that look destructive or system-changing.
SHELL_CONFIRM_PATTERNS = [
    (r"\bremove-item\b|\brm\b|\bdel\b|\berase\b|\brd\b|\brmdir\b", "deletes files"),
    (r"\bformat(-volume)?\b|\bdiskpart\b|\bclear-disk\b|\binitialize-disk\b", "wipes a disk"),
    (r"\bshutdown\b|\bstop-computer\b|\brestart-computer\b|\blogoff\b", "powers off / restarts"),
    (r"\breg\s+(add|delete|import)\b|\bset-itemproperty\b.*hk(lm|cu)|\bnew-itemproperty\b", "edits the registry"),
    (r"\bnet\s+user\b|\bnet\s+localgroup\b|\bnew-localuser\b|\badd-localgroupmember\b", "changes Windows accounts"),
    (r"\buninstall\b|\bmsiexec\b.*/x|\bwinget\s+uninstall\b|\bchoco\s+uninstall\b", "uninstalls software"),
    (r"\bvssadmin\b.*delete|\bwbadmin\b.*delete|\bcipher\b.*/w", "destroys backups / shadow copies"),
    (r"\bbcdedit\b|\bbootrec\b|\bsfc\b\s*/scannow", "touches boot configuration"),
    (r"\bset-executionpolicy\b|\bdisable-\w*(defender|firewall)\b|\bset-mppreference\b", "weakens security settings"),
    (r"\bstop-service\b|\bsc\s+delete\b|\bsc\s+config\b|\bset-service\b", "changes Windows services"),
    (r"\btaskkill\b|\bstop-process\b", "force-kills processes"),
    (r"\bschtasks\b\s*/(create|delete|change)|\bregister-scheduledtask\b", "changes scheduled tasks"),
    (r"(iwr|invoke-webrequest|curl|wget|irm|invoke-restmethod)[^|\n]*\|\s*(iex|invoke-expression)", "downloads and runs code"),
    (r"\bstart-process\b[^\n]*-verb\s+runas", "relaunches something as administrator"),
    (r"\bnetsh\b|\bnew-netfirewallrule\b", "changes network / firewall config"),
    (r"\bgit\b\s+(push|reset\s+--hard|clean\s+-\w*f)", "rewrites or publishes a git repo"),
]

_COMPILED = [(re.compile(p, re.IGNORECASE), why) for p, why in SHELL_CONFIRM_PATTERNS]


def classify(tool_name: str, args: dict):
    """Return (needs_confirmation: bool, reason: str)."""
    args = args or {}

    if tool_name in ALWAYS_CONFIRM:
        return True, {
            "write_file": "writes over a file on disk",
            "delete_path": "permanently deletes files",
            "kill_process": "force-closes a running program",
            "registry_set": "edits the Windows registry",
        }[tool_name]

    if tool_name == "forget_fact":
        return True, "permanently deletes a stored memory"

    if tool_name in ("browser_click", "browser_type"):
        return True, "interacts with a web page (clicks / types)"

    if tool_name == "power_action":
        action = str(args.get("action", "")).lower()
        if action in ("shutdown", "restart", "logoff", "hibernate"):
            return True, f"will {action} the laptop"
        return False, ""

    if tool_name == "run_powershell":
        command = str(args.get("command", ""))
        for pattern, why in _COMPILED:
            if pattern.search(command):
                return True, why
        return False, ""

    return False, ""


def describe(tool_name: str, args: dict, limit: int = 900) -> str:
    """A short human-readable preview of what is about to happen."""
    args = args or {}
    if tool_name == "run_powershell":
        body = str(args.get("command", ""))
    elif tool_name == "write_file":
        content = str(args.get("content", ""))
        preview = content[:300] + ("..." if len(content) > 300 else "")
        body = f"path: {args.get('path')}\nmode: {args.get('mode', 'overwrite')}\n---\n{preview}"
    else:
        body = "\n".join(f"{k}: {v}" for k, v in args.items())
    return body[:limit] + ("\n... (truncated)" if len(body) > limit else "")
