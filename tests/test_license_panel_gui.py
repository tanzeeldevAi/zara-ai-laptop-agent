"""Phase 4: the license panel (wizard page + settings tab), offscreen.

Driven by a fake license client — no keyring, no store, no network.
Skipped cleanly when PySide6 is not installed.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6", reason="GUI dependencies not installed")

from PySide6.QtWidgets import QApplication  # noqa: E402

from agent import licensing  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


class FakeLicenseClient:
    def __init__(self):
        self.current = licensing.LicenseStatus(
            "trial", days_left=9, detail="Free trial — 14 days, all features included.")
        self.activated_with = None
        self.refreshed_online = False

    def status(self):
        return self.current

    def refresh_online(self):
        self.refreshed_online = True
        return self.current

    def activate(self, key):
        self.activated_with = key
        self.current = licensing.LicenseStatus(
            "active", plan="pro", buyer_email="buyer@example.com",
            machines_used=1, machines_limit=2)
        return self.current


def test_license_panel_shows_status_and_activates(qapp):
    from desktop.license_panel import LicensePanel

    client = FakeLicenseClient()
    panel = LicensePanel(client=client)
    assert "Trial (9 days left)" in panel.status_label.text()
    assert "Free trial" in panel.detail_label.text()

    panel.key_edit.setText("CA1.fake.key")
    panel.activate_button.click()
    assert client.activated_with == "CA1.fake.key"
    assert "Licensed" in panel.status_label.text()
    assert "buyer@example.com" in panel.detail_label.text()
    assert panel.key_edit.text() == ""        # cleared after a good activation
    panel.close()


def test_license_panel_development_text_without_client(qapp):
    from desktop.license_panel import DEV_TEXT, LicensePanel

    panel = LicensePanel()
    assert panel.status_label.text() == DEV_TEXT
    panel.close()


def test_wizard_license_page_hosts_the_panel(qapp, tmp_path):
    from desktop.settings_model import Settings
    from desktop.wizard import LicensePage, SetupWizard

    client = FakeLicenseClient()
    wizard = SetupWizard(Settings(), env_path=tmp_path / ".env",
                         license_client=client)
    page = wizard.page(1)
    assert isinstance(page, LicensePage)
    assert page.panel._client is client
    assert "Trial (9 days left)" in page.panel.status_label.text()
    wizard.close()


def test_settings_dialog_has_a_license_tab(qapp, tmp_path):
    from desktop.settings_dialog import SettingsDialog
    from desktop.settings_model import Settings

    client = FakeLicenseClient()
    dialog = SettingsDialog(Settings(), env_path=tmp_path / ".env",
                            license_client=client)
    assert dialog.license_panel._client is client
    assert "Trial (9 days left)" in dialog.license_panel.status_label.text()
    dialog.close()
