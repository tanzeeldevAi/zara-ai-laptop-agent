"""Windows autostart toggle (Qt-free).

Creates/removes the Startup-folder shortcut exactly the way START_ZARA.bat
has always done it: a .lnk in the user's Startup folder pointing at
wscript.exe running lappilot-hidden.vbs (which launches the agent hidden).
The only deliberate change is the shortcut name — it carries the product
name now instead of "Zara".

Off Windows there is no autostart support in this build; every function
says so instead of pretending, and the wizard disables the toggle.
The PowerShell runner is injectable for tests.
"""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def supported():
    return sys.platform.startswith("win")


def _startup_dir(env=None):
    env = env if env is not None else os.environ
    appdata = env.get("APPDATA", "")
    if appdata:
        return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    return Path.home() / "AppData" / "Roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def shortcut_path(product_name="CodeHubb Assistant", env=None):
    return _startup_dir(env) / f"{product_name}.lnk"


def is_enabled(product_name="CodeHubb Assistant", env=None):
    if not supported():
        return False
    return shortcut_path(product_name, env).exists()


def _ps_quote(text):
    return "'" + str(text).replace("'", "''") + "'"


def build_create_command(root=None, product_name="CodeHubb Assistant", env=None):
    """The PowerShell one-liner (mirrors START_ZARA.bat step 3)."""
    root = Path(root) if root else ROOT
    link = shortcut_path(product_name, env)
    vbs = root / "lappilot-hidden.vbs"
    script = "; ".join(
        [
            f"$link = {_ps_quote(str(link))}",
            "$w = New-Object -ComObject WScript.Shell",
            "$s = $w.CreateShortcut($link)",
            "$s.TargetPath = 'wscript.exe'",
            f"$s.Arguments = '\"{vbs}\"'",
            f"$s.WorkingDirectory = {_ps_quote(str(root))}",
            f"$s.Description = {_ps_quote(product_name + ' laptop agent')}",
            "$s.Save()",
        ]
    )
    return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script]


def _default_runner(cmd):
    completed = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    return completed.returncode == 0


def set_enabled(enabled, root=None, product_name="CodeHubb Assistant", env=None, runner=None):
    """Toggle autostart. Returns (ok, message)."""
    if not supported():
        return False, "Autostart setup is Windows-only in this build."
    runner = runner or _default_runner
    link = shortcut_path(product_name, env)
    if enabled:
        cmd = build_create_command(root, product_name, env)
        if runner(cmd):
            return True, f"Autostart on — shortcut created at {link}."
        return False, "PowerShell could not create the Startup shortcut."
    try:
        if link.exists():
            link.unlink()
        return True, "Autostart off — Startup shortcut removed."
    except OSError as exc:
        return False, f"Could not remove the Startup shortcut: {exc}"
