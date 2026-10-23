"""CodeHubb Assistant desktop shell (Phase 2).

A PySide6 system-tray application + setup wizard around the headless agent.
All non-Qt logic (settings model, env file, audit parsing, diagnostics,
agent-process management, pairing, autostart) lives in Qt-free modules so it
can be unit-tested headlessly; only app.py / wizard.py / audit_view.py /
settings_dialog.py import PySide6.
"""
