"""Offscreen GUI smoke tests — construction only, no event loop, no display.

Skipped cleanly when PySide6 is not installed (headless agent installs).
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6", reason="GUI dependencies not installed")

from PySide6.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_tray_controller_constructs(qapp, tmp_path):
    from desktop.app import create_controller

    controller = create_controller(
        qapp, env_path=tmp_path / ".env", auto_open_wizard=False
    )
    try:
        # a fresh tmp .env bootstraps from the example -> not fully configured
        assert controller.status() in ("not_configured", "stopped")
        menu = controller.tray.contextMenu()
        texts = [a.text() for a in menu.actions()]
        assert any("Start Agent" in t for t in texts)
        assert any("Setup Wizard" in t for t in texts)
        assert any("Audit Log" in t for t in texts)
        assert any("Diagnostics" in t for t in texts)
        controller.refresh_status()
    finally:
        controller.tray.hide()


def test_wizard_constructs_with_all_pages(qapp, tmp_path):
    from desktop.settings_model import Settings
    from desktop.wizard import SetupWizard

    wizard = SetupWizard(Settings(), env_path=tmp_path / ".env")
    assert len(wizard.pageIds()) == 6
    titles = [wizard.page(i).title() for i in wizard.pageIds()]
    assert any("Welcome" in t for t in titles)
    assert any("License" in t for t in titles)
    # license page is the honest placeholder
    from PySide6.QtWidgets import QLabel

    license_page = wizard.page(1)

    texts = [label.text() for label in license_page.findChildren(QLabel)]
    assert any("Development build — no license required" in t for t in texts)
    wizard.close()


def test_settings_dialog_and_audit_view_construct(qapp, tmp_path):
    from desktop.audit_view import AuditView
    from desktop.settings_dialog import SettingsDialog
    from desktop.settings_model import Settings

    dialog = SettingsDialog(Settings(), env_path=tmp_path / ".env")
    dialog.close()

    audit_path = tmp_path / "audit.jsonl"
    audit_path.write_text(
        '{"ts": "t", "brain": "api", "tool": "screenshot", "args": "", "decision": "allowed"}\n',
        encoding="utf-8",
    )
    view = AuditView(audit_path=audit_path)
    assert view.table.rowCount() == 1
    view.filter_edit.setText("screenshot")
    assert view.table.rowCount() == 1
    view.filter_edit.setText("nomatch")
    assert view.table.rowCount() == 0
    view.close()


def test_wizard_pairing_panel_with_fake_session(qapp, tmp_path):
    from desktop.fields import TransportsFields
    from desktop.pairing import PairingStatus

    class FakeSession:
        def __init__(self):
            self.started = False
            self.stopped = False

        def start(self):
            self.started = True
            return PairingStatus("waiting_qr", qr="QRDATA", detail="Scan the QR code")

        def poll(self):
            return PairingStatus("waiting_qr", qr="QRDATA", detail="Scan the QR code")

        def stop(self):
            self.stopped = True

    fake = FakeSession()
    fields = TransportsFields(pairing_factory=lambda: fake)
    fields.start_pairing()
    assert fake.started is True
    assert fields.pair_status.text() == "Scan the QR code"
    assert fields.qr_label.pixmap() is not None
    fields.stop_pairing()
    assert fake.stopped is True
