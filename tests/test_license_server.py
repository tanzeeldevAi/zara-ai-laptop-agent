"""Phase 4: the license server + the client talking to a fake one.

The "fake server" is the real FastAPI app driven through its TestClient
(in-process, no sockets); the client's http_post is bridged onto it, so
activate/validate/revoke are exercised end to end.
"""

import hashlib
import hmac
import json
import time

import pytest

pytest.importorskip("fastapi", reason="license server dependencies not installed")

from fastapi.testclient import TestClient  # noqa: E402

from agent import licensing  # noqa: E402
from agent.store import Store  # noqa: E402
from license_server.app import ServerState, create_app  # noqa: E402
from license_server.paddle import verify_paddle_signature  # noqa: E402

DAY = 86400
LS_SECRET = "ls-test-secret"
PADDLE_SECRET = "pdl_ntfset_test_secret"


@pytest.fixture(scope="module")
def keys():
    private_pem, public_b64 = licensing.generate_keypair()
    return licensing.load_private_key(private_pem), public_b64


@pytest.fixture
def server(tmp_path, keys):
    private, public_b64 = keys
    state = ServerState(
        tmp_path / "licenses.db",
        public_key=licensing.load_public_key(public_b64),
        private_key=private,
        admin_token="admin-test-token",
        ls_secret=LS_SECRET,
        ls_variant_map={"111": {"plan": "pro", "days": 35, "machines": 2}},
        paddle_secret=PADDLE_SECRET,
        paddle_variant_map={"price_1": {"plan": "personal", "days": 35, "machines": 1}},
        paddle_email_fetcher=lambda customer_id: "paddle@buyer.example",
    )
    return TestClient(create_app(state)), state


def issue_via_admin(client, email="buyer@example.com", plan="personal", days=365,
                    machines=None):
    body = {"buyer_email": email, "plan": plan, "days": days}
    if machines is not None:
        body["machines_limit"] = machines
    response = client.post("/admin/licenses", json=body,
                           headers={"X-Admin-Token": "admin-test-token"})
    assert response.status_code == 200, response.text
    return response.json()["key"]


# ------------------------------------------------------------- admin/auth --

def test_admin_endpoints_require_the_token(server):
    client, _ = server
    assert client.post("/admin/licenses", json={}).status_code == 401
    assert client.get("/admin/licenses").status_code == 401
    key = issue_via_admin(client)
    rows = client.get("/admin/licenses",
                      headers={"X-Admin-Token": "admin-test-token"}).json()
    assert any(row["key"] == key for row in rows["licenses"])


def test_activate_validate_and_machine_limit(server, keys):
    client, _ = server
    _, public_b64 = keys
    key = issue_via_admin(client, machines=1)

    first = client.post("/activate", json={"key": key, "machine_fp": "machine-A"})
    assert first.status_code == 200
    body = first.json()
    assert body["state"] == "active" and body["machines_used"] == 1
    token_payload, error = licensing.verify_validation_token(
        body["token"], licensing.load_public_key(public_b64))
    assert error is None and token_payload["license_id"] == body["license_id"]

    same = client.post("/activate", json={"key": key, "machine_fp": "machine-A"})
    assert same.status_code == 200            # same machine: not double-counted

    other = client.post("/activate", json={"key": key, "machine_fp": "machine-B"})
    assert other.status_code == 409            # the limit is 1

    checked = client.post("/validate", json={"key": key, "machine_fp": "machine-A"})
    assert checked.json()["state"] == "active"


def test_revocation_flows_to_validate(server):
    client, _ = server
    key = issue_via_admin(client)
    payload, _ = licensing.verify_license(
        key, client.app.state.licensing.public_key)
    revoked = client.post("/admin/revoke", json={"license_id": payload["license_id"]},
                          headers={"X-Admin-Token": "admin-test-token"})
    assert revoked.status_code == 200
    checked = client.post("/validate", json={"key": key, "machine_fp": "m1"})
    assert checked.json()["state"] == "revoked"
    blocked = client.post("/activate", json={"key": key, "machine_fp": "m1"})
    assert blocked.status_code == 403


def test_client_against_fake_server_end_to_end(server, keys, tmp_path):
    client, _ = server
    _, public_b64 = keys
    key = issue_via_admin(client, machines=1)

    def bridge(url, payload):
        response = client.post(url.replace("http://licenses.test", ""), json=payload)
        return response.status_code, response.json()

    class Secrets:
        def __init__(self):
            self.data = {}

        def get(self, name, default=""):
            return self.data.get(name, default)

        def set(self, name, value):
            self.data[name] = value
            return True

    def make_manager(fingerprint):
        secrets = Secrets()
        return licensing.LicenseManager(
            Store(tmp_path / f"{fingerprint}.db"),
            public_key=public_b64, server_url="http://licenses.test",
            fingerprint=fingerprint, http_post=bridge,
            get_secret_fn=secrets.get, set_secret_fn=secrets.set)

    owner = make_manager("machine-A")
    status = owner.activate(key)
    assert status.state == "active" and status.machines_used == 1

    guest = make_manager("machine-B")
    refused = guest.activate(key)
    assert refused.allowed is False and "machine limit" in refused.detail
    assert guest.status().allowed is False       # the refusal is remembered

    # seller revokes -> the owner's next online validation shows it
    payload, _ = licensing.verify_license(key, licensing.load_public_key(public_b64))
    client.post("/admin/revoke", json={"license_id": payload["license_id"]},
                headers={"X-Admin-Token": "admin-test-token"})
    owner.refresh_online()
    assert owner.status().state == "revoked"


# --------------------------------------------------------- Lemon Squeezy --

def _ls_post(client, event, secret=LS_SECRET):
    raw = json.dumps(event).encode("utf-8")
    signature = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
    return client.post("/webhooks/lemonsqueezy", content=raw,
                       headers={"X-Signature": signature,
                                "Content-Type": "application/json"})


def test_lemonsqueezy_webhook_issues_a_verifiable_license(server, keys):
    client, _ = server
    _, public_b64 = keys
    event = {
        "meta": {"event_name": "order_created"},
        "data": {"id": "ord-1", "attributes": {
            "user_email": "ls@buyer.example", "order_id": 55,
            "first_order_item": {"variant_id": 111, "product_id": 9}}},
    }
    response = _ls_post(client, event)
    assert response.status_code == 200 and response.json()["issued"] is True
    rows = client.get("/admin/licenses", params={"email": "ls@buyer.example"},
                      headers={"X-Admin-Token": "admin-test-token"}).json()["licenses"]
    assert len(rows) == 1
    payload, error = licensing.verify_license(
        rows[0]["key"], licensing.load_public_key(public_b64))
    assert error is None and payload["plan"] == "pro" and payload["machines_limit"] == 2


def test_lemonsqueezy_bad_signature_rejected(server):
    client, _ = server
    event = {"meta": {"event_name": "order_created"}, "data": {"attributes": {}}}
    response = _ls_post(client, event, secret="the-wrong-secret")
    assert response.status_code == 401
    rows = client.get("/admin/licenses",
                      headers={"X-Admin-Token": "admin-test-token"}).json()["licenses"]
    assert rows == []


def test_lemonsqueezy_subscription_payment_extends_expiry(server):
    client, _ = server
    created = _ls_post(client, {
        "meta": {"event_name": "subscription_created"},
        "data": {"id": "sub-9", "attributes": {
            "user_email": "sub@buyer.example", "variant_id": 111}},
    })
    assert created.json()["issued"] is True
    before = client.get("/admin/licenses", params={"email": "sub@buyer.example"},
                        headers={"X-Admin-Token": "admin-test-token"}).json()["licenses"][0]
    assert before["server_expires_at"] is None
    paid = _ls_post(client, {
        "meta": {"event_name": "subscription_payment_success"},
        "data": {"id": "inv-1", "attributes": {
            "subscription_id": "sub-9", "variant_id": 111}},
    })
    assert paid.json().get("extended") == before["license_id"]
    after = client.get("/admin/licenses", params={"email": "sub@buyer.example"},
                       headers={"X-Admin-Token": "admin-test-token"}).json()["licenses"][0]
    assert after["server_expires_at"] is not None


# ---------------------------------------------------------------- Paddle --

def _paddle_header(raw, secret=PADDLE_SECRET, ts=None):
    ts = str(ts if ts is not None else int(time.time()))
    digest = hmac.new(secret.encode("utf-8"), ts.encode() + b":" + raw,
                      hashlib.sha256).hexdigest()
    return f"ts={ts};h1={digest}"


def test_paddle_signature_scheme_vectors():
    raw = b'{"event_type":"transaction.completed"}'
    header = _paddle_header(raw)
    assert verify_paddle_signature(raw, header, PADDLE_SECRET) is True
    assert verify_paddle_signature(raw, header, "another-secret") is False
    assert verify_paddle_signature(raw + b" ", header, PADDLE_SECRET) is False
    assert verify_paddle_signature(raw, "garbage", PADDLE_SECRET) is False
    # several h1 values: any single match verifies
    multi = header + ";h1=" + "0" * 64
    assert verify_paddle_signature(raw, multi, PADDLE_SECRET) is True
    # stale timestamps are rejected only when a max age is requested
    old = _paddle_header(raw, ts=int(time.time()) - 3600)
    assert verify_paddle_signature(raw, old, PADDLE_SECRET, max_age=300) is False
    assert verify_paddle_signature(raw, old, PADDLE_SECRET) is True


def test_paddle_webhook_issues_license_with_fetched_email(server, keys):
    client, _ = server
    _, public_b64 = keys
    event = {"event_type": "transaction.completed",
             "data": {"id": "txn-1", "customer_id": "ctm-1",
                      "items": [{"price": {"id": "price_1"}}]}}
    raw = json.dumps(event).encode("utf-8")
    response = client.post("/webhooks/paddle", content=raw,
                           headers={"Paddle-Signature": _paddle_header(raw),
                                    "Content-Type": "application/json"})
    assert response.status_code == 200 and response.json()["issued"] is True
    rows = client.get("/admin/licenses", params={"email": "paddle@buyer.example"},
                      headers={"X-Admin-Token": "admin-test-token"}).json()["licenses"]
    assert len(rows) == 1
    payload, error = licensing.verify_license(
        rows[0]["key"], licensing.load_public_key(public_b64))
    assert error is None and payload["plan"] == "personal"
    # a replayed webhook does not issue a second key
    replay = client.post("/webhooks/paddle", content=raw,
                         headers={"Paddle-Signature": _paddle_header(raw),
                                  "Content-Type": "application/json"})
    assert replay.json()["issued"] is False
    # and a forged one is refused outright
    forged = client.post("/webhooks/paddle", content=raw,
                         headers={"Paddle-Signature": _paddle_header(raw, secret="nope"),
                                  "Content-Type": "application/json"})
    assert forged.status_code == 401
