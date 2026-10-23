"""Diagnostics assembly, brain-connection probe, autostart command building,
WhatsApp pairing state machine, and the transport-neutral help text."""

import sys

from types import SimpleNamespace

from agent.session import HELP
from desktop import autostart, diagnostics, pairing
from desktop.brain_test import test_brain_connection as probe_brain
from desktop.settings_model import Settings


# ------------------------------------------------------------- diagnostics

def test_diagnostics_report_contains_the_bundle(tmp_path):
    report = diagnostics.build_report(
        root=tmp_path,
        problems=["DISCORD_BOT_TOKEN is empty"],
        warnings=["some warning"],
        transports=[("discord", True, False, "token or allowlist missing")],
        node_present=False,
        sidecar_ready=False,
        keyring_ok=True,
        audit_lines=['{"tool": "screenshot", "decision": "allowed"}'],
        shell_log_lines=["agent started", "transport online"],
    )
    assert "diagnostics" in report
    assert "DISCORD_BOT_TOKEN is empty" in report
    assert "some warning" in report
    assert "discord" in report and "configured=no" in report
    assert "Node.js present        : no" in report
    assert '"tool": "screenshot"' in report
    assert "transport online" in report


def test_diagnostics_handles_empty_logs(tmp_path):
    report = diagnostics.build_report(
        root=tmp_path, problems=[], warnings=[], transports=[],
        node_present=True, sidecar_ready=True, keyring_ok=False,
        audit_lines=[], shell_log_lines=[],
    )
    assert "(none)" in report
    assert "audit log is empty" in report
    assert "no shell log yet" in report


def test_diagnostics_never_prints_secret_values(tmp_path, monkeypatch):
    # Even with a token configured, only booleans may appear.
    from agent import config as config_mod

    monkeypatch.setattr(config_mod.CONFIG, "discord_token", "super-secret-token")
    report = diagnostics.build_report(root=tmp_path)
    assert "super-secret-token" not in report


# ------------------------------------------------------------ brain probe

def _fake_response():
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="OK"))]
    )


def test_brain_probe_success_with_fake_completion():
    settings = Settings(brain="api", api_provider="openai",
                        provider_model="gpt-5", openai_key="sk-test")
    seen = {}

    def fake_completion(**kwargs):
        seen.update(kwargs)
        return _fake_response()

    ok, message = probe_brain(settings, completion=fake_completion)
    assert ok is True
    assert seen["model"] == "openai/gpt-5"
    assert seen["api_key"] == "sk-test"
    assert "connection works" in message


def test_brain_probe_surfaces_provider_error():
    settings = Settings(brain="api", api_provider="gemini",
                        provider_model="gemini-2.5-pro", gemini_key="k")

    def boom(**kwargs):
        raise RuntimeError("401 invalid key")

    ok, message = probe_brain(settings, completion=boom)
    assert ok is False
    assert "401 invalid key" in message


def test_brain_probe_custom_passes_base_url():
    settings = Settings(brain="api", api_provider="custom",
                        custom_api_base="http://localhost:11434/v1",
                        provider_model="llama3.1", custom_key="")
    seen = {}

    def fake_completion(**kwargs):
        seen.update(kwargs)
        return _fake_response()

    ok, _message = probe_brain(settings, completion=fake_completion)
    assert ok is True
    assert seen["api_base"] == "http://localhost:11434/v1"
    assert "api_key" not in seen  # blank key is not sent for local servers


# -------------------------------------------------------------- autostart

def test_autostart_unsupported_off_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert autostart.supported() is False
    ok, message = autostart.set_enabled(True)
    assert ok is False
    assert "Windows-only" in message


def test_autostart_command_mirrors_bat(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    env = {"APPDATA": str(tmp_path)}
    cmd = autostart.build_create_command(root=tmp_path, env=env)
    joined = " ".join(cmd)
    assert "powershell" in cmd[0]
    assert "CreateShortcut" in joined
    assert "wscript.exe" in joined
    assert "lappilot-hidden.vbs" in joined
    assert "CodeHubb Assistant.lnk" in joined


def test_autostart_remove(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    env = {"APPDATA": str(tmp_path)}
    link = autostart.shortcut_path(env=env)
    link.parent.mkdir(parents=True)
    link.write_bytes(b"fake")
    assert autostart.is_enabled(env=env) is True
    ok, _ = autostart.set_enabled(False, env=env)
    assert ok is True
    assert not link.exists()


# ---------------------------------------------------------------- pairing

class FakeSidecarProc:
    def __init__(self):
        self._rc = None
        self.terminated = False

    def poll(self):
        return self._rc

    def terminate(self):
        self.terminated = True
        self._rc = 0

    def kill(self):
        self._rc = -9

    def wait(self, timeout=None):
        return self._rc


def _pairing(tmp_path, proc):
    (tmp_path / "sidecar" / "node_modules").mkdir(parents=True)
    session = pairing.PairingSession(root=tmp_path, node="node", popen=lambda cmd, **kw: proc)
    return session


def test_pairing_flow_qr_then_connected(tmp_path):
    proc = FakeSidecarProc()
    session = _pairing(tmp_path, proc)
    status = session.start()
    assert status.state == "starting"
    # sidecar writes a fresh QR
    session.qr_file.parent.mkdir(parents=True, exist_ok=True)
    session.qr_file.write_text("QR-PAYLOAD-123", encoding="utf-8")
    status = session.poll()
    assert status.state == "waiting_qr"
    assert status.qr == "QR-PAYLOAD-123"
    # phone links -> auth state appears
    session.auth_dir.mkdir(parents=True, exist_ok=True)
    (session.auth_dir / "creds.json").write_text("{}", encoding="utf-8")
    status = session.poll()
    assert status.state == "connected"
    session.stop()
    assert proc.terminated is True


def test_pairing_missing_node_modules_is_an_error(tmp_path):
    session = pairing.PairingSession(root=tmp_path, node="node", popen=lambda cmd, **kw: FakeSidecarProc())
    status = session.start()
    assert status.state == "error"
    assert "npm install" in status.detail


def test_pairing_logged_out_exit(tmp_path):
    proc = FakeSidecarProc()
    session = _pairing(tmp_path, proc)
    session.start()
    proc._rc = 2
    status = session.poll()
    assert status.state == "logged_out"


def test_qr_png_rendering():
    data = pairing.make_qr_png_bytes("hello-qr")
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(data) > 200


# -------------------------------------------------------------- help text

def test_help_text_is_transport_neutral():
    text = HELP.format(name="Zara")
    assert "Discord" not in text
    assert "voice note" in text.lower()
