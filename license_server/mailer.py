"""Minimal SMTP sender for license-key delivery — OPTIONAL.

Used only when SMTP_HOST is configured. When it is not, the webhook
handlers log that email is not configured and the issued key stays
retrievable by the seller via GET /admin/licenses?email=<buyer> — no key
is ever lost just because email is off.
"""

import os
import smtplib
from email.message import EmailMessage


class SmtpMailer:
    def __init__(self, host, port, username="", password="", sender="",
                 use_tls=True):
        self.host = host
        self.port = int(port)
        self.username = username
        self.password = password
        self.sender = sender or username
        self.use_tls = use_tls

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        host = (env.get("SMTP_HOST") or "").strip()
        if not host:
            return None
        return cls(
            host=host,
            port=int(env.get("SMTP_PORT") or 587),
            username=env.get("SMTP_USERNAME", ""),
            password=env.get("SMTP_PASSWORD", ""),
            sender=env.get("SMTP_FROM", ""),
            use_tls=(env.get("SMTP_TLS", "true").strip().lower() != "false"),
        )

    def send_license_email(self, to_email, key, plan):
        """Send the buyer their key. Returns True when the mail went out."""
        if not to_email:
            return False
        message = EmailMessage()
        message["Subject"] = "Your CodeHubb Assistant license key"
        message["From"] = self.sender
        message["To"] = to_email
        message.set_content(
            "Thank you for buying CodeHubb Assistant!\n\n"
            f"Your license key ({plan} plan):\n\n{key}\n\n"
            "To activate: open CodeHubb Assistant on your computer, go to\n"
            "the Setup Wizard -> License page (or Settings -> License),\n"
            "paste the key, and press Activate.\n\n"
            "Keep this email — the key is your proof of purchase.\n"
        )
        with smtplib.SMTP(self.host, self.port, timeout=30) as smtp:
            if self.use_tls:
                smtp.starttls()
            if self.username:
                smtp.login(self.username, self.password)
            smtp.send_message(message)
        return True
