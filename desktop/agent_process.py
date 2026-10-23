"""Agent process management for the desktop shell (Qt-free).

The tray app does not run the agent in-process: the agent is a long-lived
console program (run.py in the repo root) with its own threads, event loops
and single-instance mutex, so the shell spawns it as a child process and
supervises it from the outside — start, stop, exit detection, log capture.

The child's stdout/stderr are redirected straight into
.runtime/agent-shell.log (append), which is also what the diagnostics
bundle tails. The Popen factory is injectable for tests.
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class AgentProcess:
    def __init__(self, root=None, python_exe=None, log_path=None, popen=None):
        self.root = Path(root) if root else ROOT
        self.python_exe = python_exe or sys.executable
        self.log_path = (
            Path(log_path) if log_path else self.root / ".runtime" / "agent-shell.log"
        )
        self._popen = popen or subprocess.Popen
        self._proc = None
        self._log_fh = None
        self.exit_code = None

    # ------------------------------------------------------------ state

    @property
    def pid(self):
        return self._proc.pid if self._proc is not None else None

    def poll(self):
        """"running" while the child lives; "stopped" once it has exited
        (or was never started). Polling records the exit code."""
        if self._proc is None:
            return "stopped"
        rc = self._proc.poll()
        if rc is None:
            return "running"
        self.exit_code = rc
        self._close_log()
        return "stopped"

    @property
    def state(self):
        return self.poll()

    @property
    def running(self):
        return self.poll() == "running"

    # ---------------------------------------------------------- lifecycle

    def start(self):
        """Start the agent. Returns False (no-op) if it is already running."""
        if self.poll() == "running":
            return False
        self.exit_code = None
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log_fh = self.log_path.open("a", encoding="utf-8", errors="replace")
        kwargs = {
            "cwd": str(self.root),
            "stdout": self._log_fh,
            "stderr": subprocess.STDOUT,
        }
        if sys.platform.startswith("win"):
            # No console window flashing over the user's desktop.
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self._proc = self._popen([self.python_exe, "run.py"], **kwargs)
        return True

    def stop(self, timeout=5.0):
        """Terminate gracefully, then kill after `timeout`. Returns True if
        a process was actually stopped."""
        if self._proc is None or self._proc.poll() is not None:
            self.poll()
            return False
        self._proc.terminate()
        try:
            self._proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
        self.exit_code = self._proc.poll()
        self._close_log()
        return True

    # -------------------------------------------------------------- misc

    def _close_log(self):
        if self._log_fh is not None:
            try:
                self._log_fh.close()
            except Exception:  # noqa: BLE001
                pass
            self._log_fh = None

    def read_log_tail(self, lines=60):
        if not self.log_path.exists():
            return ""
        text = self.log_path.read_text(encoding="utf-8", errors="replace")
        return "\n".join(text.splitlines()[-lines:])
