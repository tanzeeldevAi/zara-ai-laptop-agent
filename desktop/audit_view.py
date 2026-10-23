"""Audit log viewer — every tool call the safety pipeline decided on."""

from pathlib import Path

from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from agent.config import CONFIG
from desktop import audit_model

DEFAULT_AUDIT_PATH = CONFIG.runtime_dir / "audit.jsonl"


class AuditView(QDialog):
    def __init__(self, parent=None, *, audit_path=None):
        super().__init__(parent)
        self.audit_path = Path(audit_path) if audit_path else DEFAULT_AUDIT_PATH
        self.setWindowTitle("Audit log — what the assistant did, and what was approved")
        self.resize(860, 520)
        self._entries = []
        self._corrupt = 0

        layout = QVBoxLayout(self)
        row = QHBoxLayout()
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Filter by tool, decision or arguments…")
        self.filter_edit.textChanged.connect(self._refilter)
        row.addWidget(self.filter_edit, 1)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh)
        row.addWidget(refresh)
        layout.addLayout(row)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Time", "Brain", "Tool", "Decision", "Arguments"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        layout.addWidget(self.table, 1)

        self.count_label = QLabel("")
        layout.addWidget(self.count_label)
        self.refresh()

    def refresh(self):
        data = audit_model.load_audit(self.audit_path)
        self._entries = data.entries
        self._corrupt = data.corrupt_lines
        self._refilter()

    def _refilter(self):
        entries = audit_model.filter_entries(self._entries, self.filter_edit.text())
        self.table.setRowCount(len(entries))
        for row, entry in enumerate(entries):
            values = [entry.ts, entry.brain, entry.tool, entry.display_decision, entry.args]
            for col, value in enumerate(values):
                self.table.setItem(row, col, QTableWidgetItem(str(value)))
        if not self._entries:
            self.count_label.setText(
                "No audit entries yet — the log appears at "
                f"{self.audit_path} once the agent runs a tool."
            )
        else:
            text = f"{len(entries)} of {len(self._entries)} entries"
            if self._corrupt:
                text += f"  •  {self._corrupt} unreadable line(s) skipped"
            self.count_label.setText(text)
