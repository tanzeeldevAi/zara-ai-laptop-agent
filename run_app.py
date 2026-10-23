#!/usr/bin/env python3
"""CodeHubb Assistant — desktop shell (system tray + setup wizard).

Run:  python run_app.py        (or: python -m desktop)
The headless agent alone is run.py; this is the friendly front door for
buyers: tray icon, first-run wizard, settings, audit log and diagnostics.
"""

from desktop.app import main

if __name__ == "__main__":
    raise SystemExit(main())
