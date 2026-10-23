"""Settings dialog — the wizard's field widgets on tabs, same model."""

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QMessageBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from desktop.fields import BehaviourFields, BrainFields, GeneralFields, TransportsFields
from desktop.license_panel import LicensePanel
from desktop.settings_model import Settings


class SettingsDialog(QDialog):
    def __init__(self, settings=None, parent=None, *, completion=None,
                 pairing_factory=None, env_path=None, license_client=None):
        super().__init__(parent)
        self.settings = settings or Settings.load(env_path)
        self.env_path = env_path
        self.save_result = None
        self.setWindowTitle(f"{self.settings.product_name} — Settings")
        self.resize(640, 560)

        layout = QVBoxLayout(self)
        tabs = QTabWidget()
        layout.addWidget(tabs)

        self.general_fields = GeneralFields()
        self.behaviour_fields = BehaviourFields()
        general_tab = QWidget()
        general_layout = QVBoxLayout(general_tab)
        general_layout.addWidget(self.general_fields)
        general_layout.addWidget(self.behaviour_fields)
        tabs.addTab(general_tab, "General")

        self.brain_fields = BrainFields(completion=completion)
        tabs.addTab(self.brain_fields, "Brain")

        self.transports_fields = TransportsFields(pairing_factory=pairing_factory)
        tabs.addTab(self.transports_fields, "Chats")

        # Phase 4: the same license panel the wizard uses. It acts
        # immediately (keyring + server), independent of Save/Cancel.
        self.license_panel = LicensePanel(client=license_client)
        tabs.addTab(self.license_panel, "License")

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        for fields in self._all_fields():
            fields.load(self.settings)

    def _all_fields(self):
        return (self.general_fields, self.behaviour_fields,
                self.brain_fields, self.transports_fields)

    def _save(self):
        notes = []
        for fields in self._all_fields():
            notes.extend(fields.apply(self.settings))
        self.save_result = self.settings.save(self.env_path)
        messages = notes + (self.save_result.warnings if self.save_result else [])
        if messages:
            QMessageBox.warning(self, "Saved with notes", "\n".join(messages))
        self.accept()

    def reject(self):
        self.transports_fields.stop_pairing()
        super().reject()

    def closeEvent(self, event):
        self.transports_fields.stop_pairing()
        super().closeEvent(event)
