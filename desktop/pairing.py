"""WhatsApp pairing helper for the setup wizard (Qt-free).

The pragmatic route chosen for Phase 2: the wizard spawns the Baileys
sidecar directly (node sidecar/index.js) with the same environment the
agent's WhatsApp transport uses, then polls the files the sidecar already
maintains — .runtime/whatsapp/qr.txt while pairing, and the auth state's
creds.json once the phone has linked. The wizard renders the QR string with
the `qrcode` package and stops the sidecar when the page is left.

The agent must NOT be running its WhatsApp transport at the same time (both
would fight over one bridge port and one auth state); the wizard page says
so, and pairing is normally done before the agent is ever started.

The Popen factory is injectable for tests.
"""

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOGGED_OUT_EXIT = 2


@dataclass
class PairingStatus:
    state: str          # starting | waiting_qr | connected | logged_out | error | stopped
    qr: str = ""        # the QR payload while state == waiting_qr
    detail: str = ""


class PairingSession:
    def __init__(self, root=None, port=17331, popen=None, node=None):
        self.root = Path(root) if root else ROOT
        self.wa_dir = self.root / ".runtime" / "whatsapp"
        self.auth_dir = self.wa_dir / "auth"
        self.qr_file = self.wa_dir / "qr.txt"
        self.port = port
        self._popen = popen or subprocess.Popen
        self._node = node  # None = resolve in start()
        self._proc = None
        self._error = ""

    def start(self):
        """Spawn the sidecar for pairing. Returns the first status."""
        if self._proc is not None and self._proc.poll() is None:
            return self.poll()
        node = self._node or shutil.which("node")
        if not node:
            self._error = (
                "Node.js was not found. Install Node.js 18+ and come back to this page."
            )
            return PairingStatus("error", detail=self._error)
        sidecar_dir = self.root / "sidecar"
        if not (sidecar_dir / "node_modules").exists():
            self._error = (
                "The WhatsApp bridge packages are not installed yet. Run "
                "`npm install` once inside the sidecar folder, then retry."
            )
            return PairingStatus("error", detail=self._error)
        self.wa_dir.mkdir(parents=True, exist_ok=True)
        # A stale QR from an earlier run must never be shown as fresh.
        try:
            self.qr_file.unlink()
        except OSError:
            pass
        env = dict(
            os.environ,
            WA_BRIDGE_PORT=str(self.port),
            WA_AUTH_DIR=str(self.auth_dir),
            WA_MEDIA_DIR=str(self.wa_dir / "media"),
            WA_QR_FILE=str(self.qr_file),
        )
        kwargs = {"cwd": str(self.root), "env": env,
                  "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if sys.platform.startswith("win"):
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self._proc = self._popen([node, str(sidecar_dir / "index.js")], **kwargs)
        return self.poll()

    def poll(self):
        if self._error and self._proc is None:
            return PairingStatus("error", detail=self._error)
        if self._proc is None:
            return PairingStatus("stopped", detail="Pairing has not been started.")
        rc = self._proc.poll()
        if rc is not None:
            if rc == LOGGED_OUT_EXIT:
                return PairingStatus(
                    "logged_out",
                    detail="WhatsApp logged this device out. Start pairing again to get a fresh QR.",
                )
            return PairingStatus("stopped", detail=f"The WhatsApp bridge stopped (exit code {rc}).")
        if (self.auth_dir / "creds.json").exists():
            return PairingStatus("connected", detail="Phone linked — WhatsApp is ready.")
        if self.qr_file.exists():
            try:
                qr = self.qr_file.read_text(encoding="utf-8").strip()
            except OSError:
                qr = ""
            if qr:
                return PairingStatus(
                    "waiting_qr", qr=qr,
                    detail="On your phone: WhatsApp → Settings → Linked devices → Link a device.",
                )
        return PairingStatus("starting", detail="Starting the WhatsApp bridge…")

    def stop(self):
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None


def make_qr_png_bytes(qr_text, box_size=8, border=2):
    """Render a QR payload to PNG bytes (used by the wizard's image label)."""
    import io

    import qrcode

    img = qrcode.make(qr_text, box_size=box_size, border=border)
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    return buffer.getvalue()
