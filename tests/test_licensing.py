"""Phase 4: the licensing core — signed keys, trial, states, seller tools.

Everything offline: keys are minted with a throwaway keypair, the clock
and the keyring are fakes, and no network is touched.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agent import licensing
from agent.store import Store

REPO = Path(__file__).resolve().parent.parent
DAY = 86400


@pytest.fixture(scope="module")
def keypair():
    private_pem, public_b64 = licensing.generate_keypair()
    return licensing.load_private_key(private_pem), public_b64


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "memory.db")


class FakeSecrets:
    def __init__(self):
        self.data = {}

    def get(self, name, default=""):
        return self.data.get(name, default)

    def set(self, name, value):
        self.data[name] = value
        return True


def make_manager(store, public_b64, clock=None, **kwargs):
    secrets = FakeSecrets()
    manager = licensing.LicenseManager(
        store,
        public_key=public_b64,
        clock=clock or time.time,
        fingerprint="fingerprint-test",
        get_secret_fn=secrets.get,
        set_secret_fn=secrets.set,
        **kwargs,
    )
    manager.fake_secrets = secrets
    return manager


def signed_key(private, public_b64, **payload_kwargs):
    payload = licensing.make_payload("buyer@example.com", **payload_kwargs)
    return licensing.sign_license(private, payload), payload


# ------------------------------------------------------------ key format --

def test_sign_verify_roundtrip(keypair):
    private, public_b64 = keypair
    key, payload = signed_key(private, public_b64, plan="pro", machines_limit=2,
                              days=30, features=["priority_support"])
    assert key.startswith("CA1.")
    parsed, error = licensing.verify_license(key, licensing.load_public_key(public_b64))
    assert error is None
    assert parsed["license_id"] == payload["license_id"]
    assert parsed["plan"] == "pro"
    assert parsed["machines_limit"] == 2
    assert parsed["features"] == ["priority_support"]


def test_tampered_payload_and_signature_rejected(keypair):
    private, public_b64 = keypair
    public = licensing.load_public_key(public_b64)
    key, _ = signed_key(private, public_b64, plan="personal")
    head, segment, signature = key.split(".")
    # alter the payload (upgrade the plan) without re-signing
    payload = json.loads(licensing._b64d(segment))
    payload["plan"] = "pro"
    forged = f"{head}.{licensing._b64e(licensing._canonical(payload))}.{signature}"
    assert licensing.verify_license(forged, public)[1] is not None
    # flip a signature character
    flipped = ("A" if signature[0] != "A" else "B") + signature[1:]
    assert licensing.verify_license(f"{head}.{segment}.{flipped}", public)[1] is not None
    # a different keypair must not verify it either
    _, other_public = licensing.generate_keypair()
    assert licensing.verify_license(key, licensing.load_public_key(other_public))[1] is not None
    # garbage in, error out (never an exception)
    assert licensing.verify_license("not a key", public)[1] is not None
    assert licensing.verify_license("", public)[1] is not None


def test_validation_token_roundtrip_and_prefix_separation(keypair):
    private, public_b64 = keypair
    public = licensing.load_public_key(public_b64)
    token = licensing.sign_validation_token(private, {"license_id": "x", "state": "active"})
    payload, error = licensing.verify_validation_token(token, public)
    assert error is None and payload["state"] == "active"
    # a token is not a license key and vice versa
    assert licensing.verify_license(token, public)[1] is not None
    key, _ = signed_key(private, public_b64)
    assert licensing.verify_validation_token(key, public)[1] is not None


def test_fingerprint_is_stable_and_input_sensitive():
    assert licensing.fingerprint_from(["a", "b", "c"]) == licensing.fingerprint_from(["a", "b", "c"])
    assert licensing.fingerprint_from(["a", "b"]) != licensing.fingerprint_from(["a", "c"])
    fp = licensing.machine_fingerprint()
    assert len(fp) == 64 and fp == licensing.machine_fingerprint()


def test_store_meta_roundtrip(store):
    assert store.get_meta("k") is None
    store.set_meta("k", "v1")
    store.set_meta("k", "v2")
    assert store.get_meta("k") == "v2"
    assert store.delete_meta("k") is True
    assert store.get_meta("k") is None


# ---------------------------------------------------------------- states --

def test_development_build_is_unrestricted(store):
    manager = licensing.LicenseManager(store, public_key="",
                                       get_secret_fn=lambda n, d="": "",
                                       set_secret_fn=lambda n, v: True)
    status = manager.status()
    assert status.state == "development"
    assert status.allowed is True
    assert licensing.status_line(status) == ""


def test_trial_math_with_injected_clock(store, keypair):
    _, public_b64 = keypair
    now = [1_700_000_000.0]
    manager = make_manager(store, public_b64, clock=lambda: now[0])
    status = manager.status()
    assert status.state == "trial" and status.days_left == 14
    now[0] += 13 * DAY
    status = manager.status()
    assert status.state == "trial" and status.days_left == 1
    now[0] += 2 * DAY        # day 15
    status = manager.status()
    assert status.state == "trial_expired" and status.allowed is False


def test_trial_record_tampering_is_detected(store, keypair):
    _, public_b64 = keypair
    now = [1_700_000_000.0]
    manager = make_manager(store, public_b64, clock=lambda: now[0])
    assert manager.status().state == "trial"
    # an attacker rolls the trial start back in the DB; the HMAC tag no
    # longer matches, so the trial reads as invalid rather than renewed
    store.set_meta(licensing.META_TRIAL_START, repr(now[0] - 365 * DAY))
    status = manager.status()
    assert status.state == "invalid" and status.allowed is False


def test_expired_and_lifetime_keys(store, keypair):
    private, public_b64 = keypair
    manager = make_manager(store, public_b64)
    old_key, _ = signed_key(private, public_b64, days=30, now=time.time() - 40 * DAY)
    manager.fake_secrets.set(licensing.SECRET_LICENSE_KEY, old_key)
    status = manager.status()
    assert status.state == "expired" and status.allowed is False

    manager2 = make_manager(store, public_b64)
    life_key, _ = signed_key(private, public_b64, days=None)
    manager2.fake_secrets.set(licensing.SECRET_LICENSE_KEY, life_key)
    status = manager2.status()
    assert status.state == "active" and status.expires_at is None


def test_grace_window_from_last_validation(store, keypair):
    private, public_b64 = keypair
    now = [1_700_000_000.0]
    manager = make_manager(store, public_b64, clock=lambda: now[0],
                           server_url="http://license.invalid")
    key, _ = signed_key(private, public_b64, days=None)
    manager.fake_secrets.set(licensing.SECRET_LICENSE_KEY, key)
    # validated an hour ago -> plain active
    store.set_meta(licensing.META_LAST_VALIDATION, repr(now[0] - 3600))
    assert manager.status().state == "active"
    # last validation 3 days ago, server unreachable since -> grace
    store.set_meta(licensing.META_LAST_VALIDATION, repr(now[0] - 3 * DAY))
    status = manager.status()
    assert status.state == "grace" and status.allowed is True
    # 9 days without a successful validation -> expired
    store.set_meta(licensing.META_LAST_VALIDATION, repr(now[0] - 9 * DAY))
    status = manager.status()
    assert status.state == "expired" and status.allowed is False


def test_revoked_state_from_server_cache(store, keypair):
    private, public_b64 = keypair
    manager = make_manager(store, public_b64)
    key, payload = signed_key(private, public_b64, days=None)
    manager.fake_secrets.set(licensing.SECRET_LICENSE_KEY, key)
    store.set_meta(licensing.META_SERVER_STATE, json.dumps(
        {"license_id": payload["license_id"], "state": "revoked"}))
    status = manager.status()
    assert status.state == "revoked" and status.allowed is False


def test_offline_activation_stores_key_and_reports(store, keypair):
    private, public_b64 = keypair

    def down(url, payload):
        raise ConnectionError("no route")

    manager = make_manager(store, public_b64, server_url="http://license.invalid",
                           http_post=down)
    key, _ = signed_key(private, public_b64, plan="pro", days=None)
    status = manager.activate(key)
    assert status.state == "active" and status.plan == "pro"
    assert "could not be reached" in status.detail
    assert manager.fake_secrets.get(licensing.SECRET_LICENSE_KEY) == key
    # a forged key is refused and NOT stored
    manager2 = make_manager(store, public_b64)
    forged = key[:-2] + ("AA" if not key.endswith("AA") else "BB")
    status = manager2.activate(forged)
    assert status.state == "invalid"
    assert manager2.fake_secrets.get(licensing.SECRET_LICENSE_KEY, "") == ""


# ------------------------------------------------------------- the seller --

def test_no_private_key_material_anywhere_in_the_repo():
    markers = ["BEGIN " + kind + " PRIVATE KEY" for kind in
               ("", "RSA ", "EC ", "OPENSSH ", "ENCRYPTED ")]
    markers = [m.replace("  ", " ") for m in markers]
    skip_dirs = {".git", ".venv", "__pycache__", "node_modules", ".runtime"}
    offenders = []
    for path in REPO.rglob("*"):
        if not path.is_file() or any(part in skip_dirs for part in path.parts):
            continue
        if path.suffix in (".db", ".pyc", ".png", ".ico", ".zip"):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="strict")
        except (UnicodeDecodeError, ValueError, OSError):
            continue
        if any(marker in text for marker in markers):
            offenders.append(str(path.relative_to(REPO)))
    assert offenders == []


def test_issue_license_tool_init_and_issue(tmp_path):
    env = dict(os.environ)
    key_out = tmp_path / "license_private.pem"
    init = subprocess.run(
        [sys.executable, "tools/issue_license.py", "--init",
         "--private-key-out", str(key_out)],
        capture_output=True, text=True, cwd=REPO, env=env)
    assert init.returncode == 0, init.stderr
    assert key_out.exists()
    public_b64 = init.stdout.strip().splitlines()[-1]

    env["LICENSE_PRIVATE_KEY_PATH"] = str(key_out)
    issued = subprocess.run(
        [sys.executable, "tools/issue_license.py", "--email", "tool@buyer.example",
         "--plan", "pro", "--days", "30", "--machines", "2"],
        capture_output=True, text=True, cwd=REPO, env=env)
    assert issued.returncode == 0, issued.stderr
    key = issued.stdout.strip().splitlines()[-1]
    payload, error = licensing.verify_license(key, licensing.load_public_key(public_b64))
    assert error is None
    assert payload["buyer_email"] == "tool@buyer.example"
    assert payload["plan"] == "pro" and payload["machines_limit"] == 2

    # --init refuses to write a private key inside the repository tree
    inside = REPO / "license_private_should_not_exist.pem"
    refused = subprocess.run(
        [sys.executable, "tools/issue_license.py", "--init",
         "--private-key-out", str(inside)],
        capture_output=True, text=True, cwd=REPO, env=env)
    assert refused.returncode == 2
    assert not inside.exists()
