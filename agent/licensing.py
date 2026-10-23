"""Licensing: signed keys, a 14-day trial, online activation, enforcement.

Phase 4. Buyer-facing model (set in the plan): the seller signs license
keys with an Ed25519 private key that NEVER ships; every release build
carries only the matching PUBLIC key (config LICENSE_PUBLIC_KEY) and can
therefore verify keys but never mint them.

Key format:  CA1.<base64url(payload_json)>.<base64url(ed25519 signature)>
Payload:     license_id, plan ("personal"|"pro"), buyer_email,
             machines_limit, issued_at (ISO), expires_at (ISO | null =
             lifetime), features (list of strings).
Validation tokens from the activation server use the same envelope with a
VT1 prefix and are signed by the same seller key.

THE development-build rule (documented, deliberate — the Phase 2 wizard
promised exactly this): if LICENSE_PUBLIC_KEY is not configured, licensing
is DISABLED and the app is unrestricted, state "development". The release
build ships with the key set; there is no other bypass, hidden or not.

State machine:
    development    no public key configured -> enforcement off, unrestricted
    trial          no key yet; 14 days from first run (HMAC-sealed record)
    trial_expired  the 14 days are used up
    active         a key verifies (offline) / server confirmed recently
    grace          server unreachable, last good validation <= 7 days old
    expired        key past its expiry, or revalidation overdue (> 7 days)
    revoked        the server says this license was revoked
    invalid        bad signature, tampered trial record, machine limit...

Honest limits, stated once here and in the report: the trial record is
tamper-EVIDENT (an HMAC keyed by the machine fingerprint), not
tamper-proof — a determined attacker can patch any purely local app.
That is accepted: licensing here keeps honest buyers honest and gives the
seller revocation + machine limits, not DRM-grade resistance.

The activated key itself lives in the OS keyring (agent/secrets.py),
never in .env or SQLite. Non-secret state (trial start, last validation,
server-reported state) lives in the SQLite store's meta table.
"""

import base64
import hashlib
import hmac
import json
import socket
import time
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

KEY_PREFIX = "CA1"
TOKEN_PREFIX = "VT1"
PLANS = ("personal", "pro")

TRIAL_DAYS = 14
GRACE_SECONDS = 7 * 86400          # offline grace after the last validation
FRESH_SECONDS = 86400              # a validation younger than this = "active"

META_TRIAL_START = "licensing.trial_start"
META_TRIAL_TAG = "licensing.trial_tag"
META_LAST_VALIDATION = "licensing.last_validation_ts"
META_SERVER_STATE = "licensing.server_state"          # JSON, see _cache_server
SECRET_LICENSE_KEY = "LICENSE_KEY"
SECRET_LICENSE_TOKEN = "LICENSE_TOKEN"

ALLOWED_STATES = ("development", "trial", "active", "grace")


# --------------------------------------------------------------- encoding --

def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    text = str(text).strip()
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _canonical(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


# ------------------------------------------------------------------ keys --

def load_public_key(value):
    """An Ed25519 public key from PEM text or raw base64 (32 bytes).

    Accepts the shapes .env can hold: PEM (with literal \\n escapes) or a
    bare base64 string. Returns None when unparseable.
    """
    if not value:
        return None
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.hazmat.primitives.serialization import load_pem_public_key

    text = str(value).strip().replace("\\n", "\n")
    if "BEGIN" in text:
        try:
            key = load_pem_public_key(text.encode("utf-8"))
            return key if isinstance(key, Ed25519PublicKey) else None
        except Exception:  # noqa: BLE001
            return None
    try:
        raw = _b64d(text)
        if len(raw) == 32:
            return Ed25519PublicKey.from_public_bytes(raw)
    except Exception:  # noqa: BLE001
        return None
    return None


def load_private_key(value):
    """An Ed25519 private key from PEM text or raw base64 (32-byte seed)."""
    if not value:
        return None
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    text = str(value).strip().replace("\\n", "\n")
    if "BEGIN" in text:
        try:
            key = load_pem_private_key(text.encode("utf-8"), password=None)
            return key if isinstance(key, Ed25519PrivateKey) else None
        except Exception:  # noqa: BLE001
            return None
    try:
        raw = _b64d(text)
        if len(raw) == 32:
            return Ed25519PrivateKey.from_private_bytes(raw)
    except Exception:  # noqa: BLE001
        return None
    return None


def generate_keypair():
    """A fresh seller keypair: (private_pem str, public_raw_base64 str)."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
        PublicFormat,
    )

    private = Ed25519PrivateKey.generate()
    private_pem = private.private_bytes(
        Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()
    ).decode("ascii")
    public_raw = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return private_pem, _b64e(public_raw)


def public_key_b64(private_key) -> str:
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    raw = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return _b64e(raw)


# ------------------------------------------------------------ sign/verify --

def _sign_envelope(private_key, prefix, payload) -> str:
    segment = _b64e(_canonical(payload))
    signature = private_key.sign(segment.encode("ascii"))
    return f"{prefix}.{segment}.{_b64e(signature)}"


def _verify_envelope(text, prefix, public_key):
    """(payload, error) — exactly one of the two is not None."""
    if public_key is None:
        return None, "no public key configured"
    parts = str(text or "").strip().split(".")
    if len(parts) != 3 or parts[0] != prefix:
        return None, f"not a {prefix} key (expected {prefix}.<payload>.<signature>)"
    try:
        payload = json.loads(_b64d(parts[1]).decode("utf-8"))
        signature = _b64d(parts[2])
    except Exception:  # noqa: BLE001
        return None, "key is malformed (undecodable payload or signature)"
    if not isinstance(payload, dict):
        return None, "key payload is not an object"
    try:
        public_key.verify(signature, parts[1].encode("ascii"))
    except Exception:  # noqa: BLE001 - InvalidSignature and friends
        return None, "signature does not verify — the key was not issued by CodeHubb (or was altered)"
    return payload, None


def sign_license(private_key, payload) -> str:
    return _sign_envelope(private_key, KEY_PREFIX, payload)


def sign_validation_token(private_key, payload) -> str:
    return _sign_envelope(private_key, TOKEN_PREFIX, payload)


def _validate_payload(payload):
    """Shape-check a license payload. Returns an error string or None."""
    if not payload.get("license_id"):
        return "payload has no license_id"
    if payload.get("plan") not in PLANS:
        return f"unknown plan: {payload.get('plan')!r}"
    if not isinstance(payload.get("buyer_email"), str):
        return "payload buyer_email is missing"
    limit = payload.get("machines_limit")
    if not isinstance(limit, int) or limit < 1:
        return "payload machines_limit must be a positive integer"
    if _parse_ts(payload.get("issued_at")) is None:
        return "payload issued_at is not a valid timestamp"
    expires = payload.get("expires_at")
    if expires is not None and _parse_ts(expires) is None:
        return "payload expires_at is not a valid timestamp"
    if not isinstance(payload.get("features", []), list):
        return "payload features must be a list"
    return None


def verify_license(key_text, public_key):
    """(payload, error): offline verification of a CA1 license key."""
    payload, error = _verify_envelope(key_text, KEY_PREFIX, public_key)
    if error:
        return None, error
    shape_error = _validate_payload(payload)
    if shape_error:
        return None, shape_error
    return payload, None


def verify_validation_token(token_text, public_key):
    """(payload, error): offline verification of a VT1 validation token."""
    return _verify_envelope(token_text, TOKEN_PREFIX, public_key)


def make_payload(buyer_email, plan="personal", machines_limit=1, days=None,
                 features=None, license_id=None, now=None):
    """Build a license payload (seller side — tools + license_server)."""
    now = time.time() if now is None else now
    expires = None
    if days is not None:
        expires = _iso(now + int(days) * 86400)
    return {
        "license_id": license_id or uuid.uuid4().hex,
        "plan": plan,
        "buyer_email": buyer_email or "",
        "machines_limit": int(machines_limit),
        "issued_at": _iso(now),
        "expires_at": expires,
        "features": list(features or []),
    }


# -------------------------------------------------------------- timestamps --

def _iso(epoch) -> str:
    return datetime.fromtimestamp(float(epoch), tz=timezone.utc).isoformat()


def _parse_ts(value):
    """ISO string (or epoch number) -> epoch seconds, or None."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (ValueError, OverflowError):
        return None


# ------------------------------------------------------------- fingerprint --

def _machine_guid():
    """Windows MachineGuid from the registry; "" anywhere else/on failure."""
    try:
        import sys

        if sys.platform != "win32":
            return ""
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography"
        ) as key:
            value, _ = winreg.QueryValueEx(key, "MachineGuid")
            return str(value)
    except Exception:  # noqa: BLE001
        return ""


def fingerprint_parts():
    """The stable identifiers this machine's fingerprint is hashed from."""
    node = uuid.getnode()
    mac = ":".join(f"{(node >> shift) & 0xFF:02x}" for shift in range(40, -1, -8))
    return [_machine_guid(), socket.gethostname() or "", mac]


def fingerprint_from(parts) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()


def machine_fingerprint() -> str:
    """SHA-256 of this machine's stable identifiers. Only the hash is ever
    stored or sent — never the identifiers themselves."""
    return fingerprint_from(fingerprint_parts())


# --------------------------------------------------------------- transport --

def urllib_http_post(url, payload, timeout=10):
    """Default server client: POST JSON, return (status, parsed_body).

    Raises OSError on any network failure — callers treat that as
    "server unreachable" and fall back to the offline rules.
    """
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        try:
            parsed = json.loads(exc.read().decode("utf-8") or "{}")
        except Exception:  # noqa: BLE001
            parsed = {}
        return exc.code, parsed


# ----------------------------------------------------------------- status --

@dataclass
class LicenseStatus:
    state: str
    detail: str = ""
    plan: str = ""
    buyer_email: str = ""
    license_id: str = ""
    expires_at: float = None          # epoch; None = lifetime / n-a
    days_left: int = None             # trial days, or days to expiry
    machines_used: int = None
    machines_limit: int = None
    features: list = field(default_factory=list)

    @property
    def allowed(self) -> bool:
        return self.state in ALLOWED_STATES


def status_line(status) -> str:
    """One short line for the tray / startup banner."""
    if status.state == "development":
        return ""
    if status.state == "trial":
        return f"Trial ({status.days_left} day{'s' if status.days_left != 1 else ''} left)"
    if status.state == "active":
        return f"Licensed · {status.plan}" if status.plan else "Licensed"
    if status.state == "grace":
        return "Licensed · offline grace period"
    if status.state == "trial_expired":
        return "Trial expired — activation required"
    if status.state == "expired":
        return "License expired — activation required"
    if status.state == "revoked":
        return "License revoked — activation required"
    return "License invalid — activation required"


def activation_notice(status) -> str:
    """The short text sessions answer with while activation is required."""
    reason = {
        "trial_expired": "your free trial has ended",
        "expired": "your license has expired",
        "revoked": "this license has been revoked",
        "invalid": "no valid license was found on this machine",
    }.get(status.state, "this copy is not activated")
    return (
        "🔒 CodeHubb Assistant needs activation — "
        f"{reason}.\n"
        "Your data and settings are safe; the assistant just can't work until "
        "a license is active.\n"
        "To activate: open the CodeHubb Assistant app on this computer → "
        "Setup Wizard → License (or Settings → License) and paste your key.\n"
        "No key yet? Buy one at codehubb.com — the key arrives by email.\n"
        "Send /license any time to see the current license status."
    )


def license_text(status) -> str:
    """The fuller /license reply."""
    if status.state == "development":
        return (
            "License: development build — licensing is not enforced on this "
            "build and no key is required."
        )
    lines = [f"License status: {status.state}"]
    if status.plan:
        lines.append(f"Plan: {status.plan}")
    if status.buyer_email:
        lines.append(f"Licensed to: {status.buyer_email}")
    if status.state == "trial":
        lines.append(f"Trial: {status.days_left} day(s) left of {TRIAL_DAYS}")
    if status.expires_at is not None:
        when = datetime.fromtimestamp(status.expires_at, tz=timezone.utc).date().isoformat()
        lines.append(f"Expires: {when} ({status.days_left} day(s) left)")
    elif status.license_id:
        lines.append("Expires: never (lifetime license)")
    if status.machines_limit is not None:
        used = status.machines_used if status.machines_used is not None else "?"
        lines.append(f"Machines: {used} of {status.machines_limit} used")
    if status.detail:
        lines.append(status.detail)
    if not status.allowed:
        lines.append("Open the desktop app → Settings → License to activate.")
    return "\n".join(lines)


# ---------------------------------------------------------------- manager --

class LicenseManager:
    """Computes license state from the keyring key + store meta + server.

    Everything network-shaped goes through the injectable `http_post`
    callable: (url, payload_dict) -> (status_code, body_dict), raising
    OSError when the server is unreachable. status() itself NEVER touches
    the network — it works from the cached validation in the store meta;
    refresh_online() is the only method that calls the server (beyond
    activate(), which activates).
    """

    def __init__(self, store, *, public_key="", server_url="", clock=None,
                 fingerprint=None, http_post=None, get_secret_fn=None,
                 set_secret_fn=None):
        from . import secrets as secrets_mod

        self.store = store
        self.public_key_text = public_key or ""
        self._public_key = load_public_key(self.public_key_text)
        self.server_url = (server_url or "").rstrip("/")
        self.clock = clock or time.time
        self.fingerprint = fingerprint or machine_fingerprint()
        self.http_post = http_post or urllib_http_post
        self._get_secret = get_secret_fn or secrets_mod.get_secret
        self._set_secret = set_secret_fn or secrets_mod.set_secret
        self._memory_key = ""      # fallback when the keyring is unavailable

    # ------------------------------------------------------------ basics --

    @property
    def enforcement_enabled(self) -> bool:
        return self._public_key is not None

    def _now(self):
        return float(self.clock())

    def _stored_key(self):
        if self._memory_key:
            return self._memory_key
        try:
            return self._get_secret(SECRET_LICENSE_KEY, "") or ""
        except Exception:  # noqa: BLE001
            return ""

    # ------------------------------------------------------------- status --

    def status(self) -> LicenseStatus:
        if not self.enforcement_enabled:
            return LicenseStatus(
                "development",
                detail="No LICENSE_PUBLIC_KEY is configured, so this is a "
                       "development build and licensing is disabled.",
            )
        key = self._stored_key()
        if key:
            return self._key_status(key)
        return self._trial_status()

    def allowed(self) -> bool:
        return self.status().allowed

    def _key_status(self, key) -> LicenseStatus:
        payload, error = verify_license(key, self._public_key)
        if error:
            return LicenseStatus("invalid", detail=error)
        server = self._cached_server_state()
        if server.get("license_id") not in (None, payload["license_id"]):
            server = {}      # cache belongs to a different key: ignore it
        if server.get("state") == "refused":
            return LicenseStatus(
                "invalid",
                detail="This license could not be activated on this machine "
                       "(its machine limit is already used up).",
                plan=payload["plan"], buyer_email=payload["buyer_email"],
                license_id=payload["license_id"],
                machines_limit=payload["machines_limit"],
                machines_used=server.get("machines_used"),
            )
        if server.get("state") == "revoked":
            return LicenseStatus(
                "revoked",
                detail="The license server reports this license as revoked.",
                plan=payload["plan"], buyer_email=payload["buyer_email"],
                license_id=payload["license_id"],
                machines_limit=payload["machines_limit"],
                machines_used=server.get("machines_used"),
            )
        expires = _parse_ts(payload.get("expires_at"))
        server_expires = _parse_ts(server.get("expires_at"))
        if server_expires is not None:
            expires = server_expires
        now = self._now()
        machines_used = server.get("machines_used")
        if expires is not None and now > expires:
            return LicenseStatus(
                "expired",
                detail="The license period has ended. Renew to keep using "
                       "CodeHubb Assistant; your data stays on this machine.",
                plan=payload["plan"], buyer_email=payload["buyer_email"],
                license_id=payload["license_id"], expires_at=expires,
                days_left=0, machines_used=machines_used,
                machines_limit=payload["machines_limit"],
                features=payload.get("features", []),
            )
        state, detail = "active", ""
        if self.server_url:
            last = self._last_validation()
            if last is not None:
                age = now - last
                if age > GRACE_SECONDS:
                    return LicenseStatus(
                        "expired",
                        detail="This license could not be re-validated online "
                               "for over 7 days. Connect to the internet so it "
                               "can check in, or contact support.",
                        plan=payload["plan"], buyer_email=payload["buyer_email"],
                        license_id=payload["license_id"], expires_at=expires,
                        machines_used=machines_used,
                        machines_limit=payload["machines_limit"],
                        features=payload.get("features", []),
                    )
                if age > FRESH_SECONDS:
                    state = "grace"
                    detail = ("The license server could not be reached recently; "
                              "running on the offline grace period.")
        return LicenseStatus(
            state,
            detail=detail,
            plan=payload["plan"], buyer_email=payload["buyer_email"],
            license_id=payload["license_id"], expires_at=expires,
            days_left=(max(0, int((expires - now) // 86400)) if expires else None),
            machines_used=machines_used,
            machines_limit=payload["machines_limit"],
            features=payload.get("features", []),
        )

    def _trial_status(self) -> LicenseStatus:
        now = self._now()
        raw = self.store.get_meta(META_TRIAL_START)
        if raw is not None:
            tag = self.store.get_meta(META_TRIAL_TAG) or ""
            if not hmac.compare_digest(tag, self._trial_tag(raw)):
                return LicenseStatus(
                    "invalid",
                    detail="The trial record failed its integrity check, so "
                           "the trial cannot be trusted. Activate a license "
                           "key, or contact support.",
                )
            try:
                start = float(raw)
            except (TypeError, ValueError):
                return LicenseStatus(
                    "invalid", detail="The trial record is unreadable."
                )
        else:
            # First run under enforcement: the trial starts now.
            start = now
            self.store.set_meta(META_TRIAL_START, repr(start))
            self.store.set_meta(META_TRIAL_TAG, self._trial_tag(repr(start)))
        elapsed_days = int((now - start) // 86400)
        if elapsed_days >= TRIAL_DAYS:
            return LicenseStatus(
                "trial_expired",
                detail=f"The {TRIAL_DAYS}-day free trial has ended.",
                days_left=0,
            )
        return LicenseStatus(
            "trial",
            detail=f"Free trial — {TRIAL_DAYS} days, all features included.",
            days_left=TRIAL_DAYS - elapsed_days,
        )

    def _trial_tag(self, raw_start) -> str:
        return hmac.new(
            self.fingerprint.encode("utf-8"),
            f"trial:{raw_start}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    # ----------------------------------------------------- server contact --

    def _cached_server_state(self):
        raw = self.store.get_meta(META_SERVER_STATE)
        if not raw:
            return {}
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except (TypeError, ValueError):
            return {}

    def _last_validation(self):
        raw = self.store.get_meta(META_LAST_VALIDATION)
        try:
            return float(raw) if raw is not None else None
        except (TypeError, ValueError):
            return None

    def _cache_server(self, body, token=""):
        state = {
            "license_id": body.get("license_id"),
            "state": body.get("state", "active"),
            "plan": body.get("plan", ""),
            "expires_at": body.get("expires_at"),
            "machines_used": body.get("machines_used"),
            "machines_limit": body.get("machines_limit"),
        }
        self.store.set_meta(META_SERVER_STATE, json.dumps(state))
        self.store.set_meta(META_LAST_VALIDATION, repr(self._now()))
        if token:
            try:
                self._set_secret(SECRET_LICENSE_TOKEN, token)
            except Exception:  # noqa: BLE001
                pass

    def _server_call(self, path, payload):
        """(status, body) or None when the server is unreachable/untrusted."""
        try:
            code, body = self.http_post(f"{self.server_url}{path}", payload)
        except Exception:  # noqa: BLE001 - any network failure = unreachable
            return None
        if not isinstance(body, dict):
            return None
        token = body.get("token")
        if token:
            _payload, error = verify_validation_token(token, self._public_key)
            if error:
                return None      # a response we cannot trust: treat as down
        return code, body

    def refresh_online(self):
        """Ask the server for the current state; update the cache.

        Returns the fresh LicenseStatus, or None when the server could not
        be reached (callers then rely on status()'s offline rules)."""
        if not self.server_url or not self.enforcement_enabled:
            return None
        key = self._stored_key()
        if not key:
            return None
        try:
            token = self._get_secret(SECRET_LICENSE_TOKEN, "") or ""
        except Exception:  # noqa: BLE001
            token = ""
        result = self._server_call(
            "/validate",
            {"key": key, "machine_fp": self.fingerprint, "token": token},
        )
        if result is None:
            return None
        _code, body = result
        self._cache_server(body, token=body.get("token", ""))
        return self.status()

    def activate(self, key_text) -> LicenseStatus:
        """Verify + store a pasted key, then activate online when possible."""
        if not self.enforcement_enabled:
            return LicenseStatus(
                "development",
                detail="This build has licensing disabled — no key is needed.",
            )
        key = str(key_text or "").strip()
        payload, error = verify_license(key, self._public_key)
        if error:
            return LicenseStatus("invalid", detail=error)
        stored = False
        try:
            stored = bool(self._set_secret(SECRET_LICENSE_KEY, key))
        except Exception:  # noqa: BLE001
            stored = False
        if stored:
            self._memory_key = ""
        else:
            # No working keyring: keep the key for this process only and say
            # so — silently writing it to .env/SQLite would break the rule
            # that license keys live in the keyring.
            self._memory_key = key
        base = self._key_status(key)
        if base.state == "revoked" or base.state == "expired":
            return base
        note = "" if stored else (
            " Note: the OS keyring is unavailable, so the key is kept for "
            "this session only — set LICENSE_KEY in .env to persist it."
        )
        if not self.server_url:
            if note:
                base.detail = (base.detail + " " + note).strip()
            return base
        result = self._server_call(
            "/activate", {"key": key, "machine_fp": self.fingerprint}
        )
        if result is None:
            base.detail = (
                (base.detail + " " if base.detail else "")
                + "The license server could not be reached, so activation "
                  "will complete when it is back."
                + note
            ).strip()
            return base
        code, body = result
        if code == 200 and body.get("state") == "active":
            self._cache_server(body, token=body.get("token", ""))
            fresh = self.status()
            if note:
                fresh.detail = (fresh.detail + " " + note).strip()
            return fresh
        # Refused (machine limit, revoked, ...): remember the refusal so a
        # later status() doesn't quietly treat the key as active offline.
        self.store.set_meta(
            META_SERVER_STATE,
            json.dumps({
                "license_id": payload["license_id"],
                "state": "refused",
                "machines_used": body.get("machines_used"),
            }),
        )
        return LicenseStatus(
            "invalid",
            detail=body.get("detail")
            or f"The license server refused activation (HTTP {code}).",
            plan=payload["plan"], buyer_email=payload["buyer_email"],
            license_id=payload["license_id"],
            machines_limit=payload["machines_limit"],
            machines_used=body.get("machines_used"),
        )


# ---------------------------------------------------------------- factory --

def get_manager(store=None, *, config=None):
    """A LicenseManager wired to the app config (LICENSE_PUBLIC_KEY /
    LICENSE_SERVER_URL). Cheap to construct: no I/O until status()."""
    if config is None:
        from .config import CONFIG as config
    if store is None:
        from .store import get_store

        store = get_store()
    return LicenseManager(
        store,
        public_key=config.license_public_key,
        server_url=config.license_server_url,
    )


def enforcement_enabled(config=None) -> bool:
    """True when this build carries a license public key (release build)."""
    if config is None:
        from .config import CONFIG as config
    return load_public_key(config.license_public_key) is not None
