"""Tray application — the desktop shell around the headless agent.

Lives in the system tray: status at a glance, start/stop the agent
subprocess, open the setup wizard, settings, the audit log and diagnostics.
First run (no .env yet, or configuration problems) opens the wizard
automatically. Quitting the shell stops the agent.
"""

import sys

from PySide6.QtCore import QTimer
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from desktop import diagnostics, icons
from desktop.agent_process import AgentProcess
from desktop.audit_view import AuditView
from desktop.settings_dialog import SettingsDialog
from desktop.settings_model import Settings
from desktop.wizard import SetupWizard


class DiagnosticsDialog(QDialog):
    """The support bundle: read it, copy it, or save it as a .txt file."""

    def __init__(self, report, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Diagnostics — send this with a problem report")
        self.resize(720, 560)
        layout = QVBoxLayout(self)
        self.text = QPlainTextEdit(report)
        self.text.setReadOnly(True)
        layout.addWidget(self.text, 1)
        row = QHBoxLayout()
        copy_button = QPushButton("Copy to clipboard")
        copy_button.clicked.connect(
            lambda: QApplication.clipboard().setText(self.text.toPlainText())
        )
        row.addWidget(copy_button)
        save_button = QPushButton("Save as .txt…")
        save_button.clicked.connect(self._save)
        row.addWidget(save_button)
        row.addStretch(1)
        layout.addLayout(row)

    def _save(self):
        from PySide6.QtWidgets import QFileDialog

        path, _chosen = QFileDialog.getSaveFileName(
            self, "Save diagnostics", "codehubb-assistant-diagnostics.txt", "Text files (*.txt)"
        )
        if path:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(self.text.toPlainText())


class TrayController(QWidget):
    """Owns the tray icon, the agent process and every window it opens."""

    def __init__(self, app, *, env_path=None, auto_open_wizard=True):
        super().__init__()
        self.app = app
        self.env_path = env_path
        icons.ensure_icons()
        self.agent = AgentProcess()
        self._last_exit_code = None
        self._error_latched = False
        self.settings = self._load_settings()

        self.tray = QSystemTrayIcon(self._icon_for("stopped"), self)
        self.tray.setToolTip(self.settings.product_name or "CodeHubb Assistant")
        menu = QMenu()
        self.status_action = QAction("Status: …", menu)
        self.status_action.setEnabled(False)
        menu.addAction(self.status_action)
        menu.addSeparator()
        self.start_action = QAction("Start Agent", menu)
        self.start_action.triggered.connect(self.start_agent)
        menu.addAction(self.start_action)
        self.stop_action = QAction("Stop Agent", menu)
        self.stop_action.triggered.connect(self.stop_agent)
        menu.addAction(self.stop_action)
        menu.addSeparator()
        wizard_action = QAction("Setup Wizard…", menu)
        wizard_action.triggered.connect(self.open_wizard)
        menu.addAction(wizard_action)
        settings_action = QAction("Settings…", menu)
        settings_action.triggered.connect(self.open_settings)
        menu.addAction(settings_action)
        audit_action = QAction("Audit Log…", menu)
        audit_action.triggered.connect(self.open_audit)
        menu.addAction(audit_action)
        diag_action = QAction("Run Diagnostics", menu)
        diag_action.triggered.connect(self.open_diagnostics)
        menu.addAction(diag_action)
        menu.addSeparator()
        quit_action = QAction("Quit", menu)
        quit_action.triggered.connect(self.quit)
        menu.addAction(quit_action)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_tray_click)
        self.tray.show()

        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self.refresh_status)
        self._timer.start()
        self.refresh_status()

        if auto_open_wizard and self._needs_setup():
            QTimer.singleShot(300, self.open_wizard)

    # ------------------------------------------------------------- state

    def _load_settings(self):
        try:
            return Settings.load(self.env_path)
        except Exception:  # noqa: BLE001 - shell must still open on a broken .env
            return Settings()

    def _needs_setup(self):
        return self.settings.fresh_install or bool(self.settings.validate())

    def _problems(self):
        try:
            return self.settings.validate()
        except Exception:  # noqa: BLE001
            return ["configuration could not be read"]

    def status(self):
        if self.agent.state == "running":
            return "running"
        if self._error_latched:
            return "error"
        if self._problems():
            return "not_configured"
        return "stopped"

    def refresh_status(self):
        previous = self.agent.state
        self.agent.poll()
        if previous == "running" and self.agent.state == "stopped":
            self._last_exit_code = self.agent.exit_code
            # exit 0 = clean shutdown, 3 = another instance already running;
            # anything else is a crash worth surfacing.
            self._error_latched = self._last_exit_code not in (0, 3, None)
        state = self.status()
        label = {
            "running": f"Running (PID {self.agent.pid})",
            "stopped": "Stopped",
            "not_configured": "Not configured — open the Setup Wizard",
            "error": f"Error — agent exited with code {self._last_exit_code}",
        }[state]
        name = self.settings.assistant_name or "Assistant"
        suffix = self._license_suffix()
        if suffix:
            label = f"{label} · {suffix}"
        self.status_action.setText(f"{name}: {label}")
        self.tray.setIcon(self._icon_for(state))
        self.start_action.setEnabled(state != "running")
        self.stop_action.setEnabled(state == "running")

    _license_cache = ("", 0.0)

    def _license_suffix(self):
        """'Trial (9 days left)' / 'Activation required' — release builds
        only; development builds append nothing. Cached: the tray refreshes
        every second, license state does not change that fast."""
        import time

        text, stamp = self._license_cache
        if time.monotonic() - stamp < 30:
            return text
        text = ""
        try:
            from agent import licensing

            if licensing.enforcement_enabled():
                status = licensing.get_manager().status()
                text = licensing.status_line(status)
                if status.state in ("trial_expired", "expired", "revoked", "invalid"):
                    text = "Activation required"
        except Exception:  # noqa: BLE001 - the tray must never die on this
            text = ""
        self._license_cache = (text, time.monotonic())
        return text

    def _icon_for(self, state):
        filename = {
            "running": "icon.png",
            "error": "icon-error.png",
        }.get(state, "icon-stopped.png")
        return QIcon(str(icons.ASSETS_DIR / filename))

    def _on_tray_click(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            if self.status() == "running":
                self.stop_agent()
            else:
                self.start_agent()

    # ----------------------------------------------------------- actions

    def start_agent(self):
        problems = self._problems()
        if problems:
            QMessageBox.information(
                self, "Setup needed first",
                "The assistant is not fully configured yet:\n\n"
                + "\n".join(f"• {p}" for p in problems)
                + "\n\nOpening the setup wizard…",
            )
            self.open_wizard()
            return
        self._error_latched = False
        self.agent.start()
        self.refresh_status()

    def stop_agent(self):
        self.agent.stop()
        self._error_latched = False
        self.refresh_status()

    def open_wizard(self):
        wizard = SetupWizard(self._load_settings(), self, env_path=self.env_path)
        if wizard.exec():
            self.settings = self._load_settings()
            warnings = wizard.save_result.warnings if wizard.save_result else []
            if warnings:
                QMessageBox.warning(self, "Saved with notes", "\n".join(warnings))
            if wizard.start_agent_requested:
                self.start_agent()
            self.refresh_status()

    def open_settings(self):
        dialog = SettingsDialog(self._load_settings(), self, env_path=self.env_path)
        if dialog.exec():
            self.settings = self._load_settings()
            self.refresh_status()

    def open_audit(self):
        view = AuditView(self)
        view.exec()

    def open_diagnostics(self):
        problems = self._problems()
        warnings = []
        try:
            warnings = self.settings.validate_warnings()
        except Exception:  # noqa: BLE001
            warnings = []
        report = diagnostics.build_report(problems=problems, warnings=warnings)
        DiagnosticsDialog(report, self).exec()

    def quit(self):
        if self.agent.state == "running":
            self.agent.stop()
        self.tray.hide()
        self.app.quit()


def create_controller(app=None, **kwargs):
    """Build the tray controller (used by main() and by the smoke tests)."""
    app = app or QApplication.instance() or QApplication(sys.argv)
    return TrayController(app, **kwargs)


def main(argv=None):
    app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName("CodeHubb Assistant")
    app.setQuitOnLastWindowClosed(False)
    controller = create_controller(app)
    _ = controller  # kept alive by its tray icon parent chain + app reference below
    app._tray_controller = controller
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
