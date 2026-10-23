"""License panel — status + activation, shared by wizard and settings.

Phase 4 turns the Phase 2 placeholder into the real thing: the current
license state (plan, buyer, expiry, machines), a key paste field, and an
Activate button. All logic lives in agent/licensing.py; this widget only
talks to an injectable client (status() / activate(key) /
refresh_online()) so tests drive it with a fake — no keyring, no network.
"""

from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from agent import licensing

DEV_TEXT = "Development build — no license required"


class ManagerLicenseClient:
    """The default client: a LicenseManager on the app config + store."""

    def __init__(self):
        self._manager = None

    def _mgr(self):
        if self._manager is None:
            self._manager = licensing.get_manager()
        return self._manager

    def status(self):
        return self._mgr().status()

    def activate(self, key):
        return self._mgr().activate(key)

    def refresh_online(self):
        try:
            self._mgr().refresh_online()
        except Exception:  # noqa: BLE001 - offline is a normal state
            pass
        return self._mgr().status()


class LicensePanel(QWidget):
    def __init__(self, parent=None, *, client=None):
        super().__init__(parent)
        self._client = client
        layout = QVBoxLayout(self)

        intro = QLabel(
            "CodeHubb Assistant is licensed per computer. When you buy, your "
            "key arrives by email — paste it below and press Activate. "
            "A fresh install runs a 14-day free trial with every feature."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.status_label = QLabel("")
        self.status_label.setStyleSheet("font-weight: bold;")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.detail_label = QLabel("")
        self.detail_label.setWordWrap(True)
        layout.addWidget(self.detail_label)

        layout.addWidget(QLabel("License key:"))
        row = QHBoxLayout()
        self.key_edit = QLineEdit()
        self.key_edit.setPlaceholderText("CA1.…")
        row.addWidget(self.key_edit, 1)
        self.activate_button = QPushButton("Activate")
        self.activate_button.clicked.connect(self._activate)
        row.addWidget(self.activate_button)
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(lambda: self.refresh(online=True))
        row.addWidget(self.refresh_button)
        layout.addLayout(row)
        layout.addStretch(1)

        if client is not None:
            self.refresh()
        else:
            # Cheap initial paint without touching the store/keyring: the
            # full status loads when the page is actually shown/used.
            try:
                enforced = licensing.enforcement_enabled()
            except Exception:  # noqa: BLE001
                enforced = False
            self.status_label.setText(
                "License status — press Refresh" if enforced else DEV_TEXT
            )

    # ------------------------------------------------------------- client --

    def _get_client(self):
        if self._client is None:
            self._client = ManagerLicenseClient()
        return self._client

    def refresh(self, online=False):
        try:
            client = self._get_client()
            refresh_online = getattr(client, "refresh_online", None)
            if online and callable(refresh_online):
                status = refresh_online()
            else:
                status = client.status()
        except Exception as exc:  # noqa: BLE001 - the panel must never crash the shell
            self.status_label.setText("License status unavailable")
            self.detail_label.setText(str(exc))
            return
        self._render(status)

    def _activate(self):
        key = self.key_edit.text().strip()
        if not key:
            self.detail_label.setText("Paste your license key first — it starts with CA1.")
            return
        try:
            status = self._get_client().activate(key)
        except Exception as exc:  # noqa: BLE001
            self.status_label.setText("Activation failed")
            self.detail_label.setText(str(exc))
            return
        if status.allowed:
            self.key_edit.clear()
        self._render(status)

    def _render(self, status):
        if status.state == "development":
            self.status_label.setText(DEV_TEXT)
        else:
            self.status_label.setText(
                f"License: {licensing.status_line(status) or status.state}"
            )
        self.detail_label.setText(licensing.license_text(status))
