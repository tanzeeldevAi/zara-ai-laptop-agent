"""The CodeHubb Assistant license server (FastAPI).

Seller-hosted. Buyers' apps talk to two endpoints:

    POST /activate   {key, machine_fp}            -> signed VT1 token + state
    POST /validate   {key, machine_fp, token}     -> current state (revocation!)

Payments become licenses through webhooks:

    POST /webhooks/lemonsqueezy   order/subscription events -> issue + email
    POST /webhooks/paddle         transaction.completed     -> issue + email

and the seller manages licenses with an admin token (X-Admin-Token):

    POST /admin/licenses          issue a key by hand (JazzCash/bank sales)
    POST /admin/revoke            revoke by license_id or key
    GET  /admin/licenses?email=   list (incl. keys — support retrieval)

Configuration is all environment variables — see README.md. The server
holds the seller's Ed25519 keypair: the PUBLIC key verifies buyer keys,
the PRIVATE key (when configured) signs validation tokens and issues new
licenses. The private key never leaves this server.

Run:  uvicorn license_server.app:app --host 0.0.0.0 --port 8787
"""

import hashlib
import hmac
import json
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import licensing  # noqa: E402
from license_server import paddle as paddle_mod  # noqa: E402
from license_server.mailer import SmtpMailer  # noqa: E402

TOKEN_TTL_SECONDS = 30 * 86400     # validation tokens live 30 days
LS_PERIOD_DAYS_DEFAULT = 35        # monthly subscription + slack

_SCHEMA = """
CREATE TABLE IF NOT EXISTS licenses(
    license_id TEXT PRIMARY KEY,
    key TEXT NOT NULL,
    payload TEXT NOT NULL,
    buyer_email TEXT NOT NULL DEFAULT '',
    plan TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    machines_limit INTEGER NOT NULL DEFAULT 1,
    server_expires_at TEXT,
    ls_subscription_id TEXT,
    ls_order_id TEXT,
    paddle_transaction_id TEXT,
    paddle_customer_id TEXT,
    created_ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS activations(
    license_id TEXT NOT NULL,
    machine_fp TEXT NOT NULL,
    activated_at REAL NOT NULL,
    last_seen REAL NOT NULL,
    PRIMARY KEY(license_id, machine_fp)
);
"""


class ServerState:
    """Config + SQLite handle for one server instance (thread-safe)."""

    def __init__(self, db_path, *, public_key=None, private_key=None,
                 admin_token="", ls_secret="", ls_variant_map=None,
                 paddle_secret="", paddle_variant_map=None, mailer=None,
                 paddle_email_fetcher=None, clock=None):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.public_key = public_key          # Ed25519 public key object
        self.private_key = private_key        # Ed25519 private key object
        self.admin_token = admin_token or ""
        self.ls_secret = ls_secret or ""
        self.ls_variant_map = ls_variant_map or {}
        self.paddle_secret = paddle_secret or ""
        self.paddle_variant_map = paddle_variant_map or {}
        self.mailer = mailer
        self.paddle_email_fetcher = paddle_email_fetcher
        self.clock = clock or time.time
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # ----------------------------------------------------------- licenses --

    def insert_license(self, key, payload, **extra):
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO licenses(license_id, key, payload, "
                "buyer_email, plan, status, machines_limit, server_expires_at, "
                "ls_subscription_id, ls_order_id, paddle_transaction_id, "
                "paddle_customer_id, created_ts) "
                "VALUES (?,?,?,?,?,'active',?,?,?,?,?,?,?)",
                (
                    payload["license_id"], key, json.dumps(payload),
                    payload.get("buyer_email", ""), payload.get("plan", ""),
                    payload.get("machines_limit", 1),
                    extra.get("server_expires_at"),
                    extra.get("ls_subscription_id"), extra.get("ls_order_id"),
                    extra.get("paddle_transaction_id"),
                    extra.get("paddle_customer_id"), self.clock(),
                ),
            )
            self._conn.commit()

    def get_license(self, license_id):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM licenses WHERE license_id = ?", (license_id,)
            ).fetchone()
        return dict(row) if row else None

    def find_license(self, **where):
        clauses = " AND ".join(f"{name} = ?" for name in where)
        with self._lock:
            row = self._conn.execute(
                f"SELECT * FROM licenses WHERE {clauses} ORDER BY created_ts LIMIT 1",
                tuple(where.values()),
            ).fetchone()
        return dict(row) if row else None

    def list_licenses(self, email=None):
        with self._lock:
            if email:
                rows = self._conn.execute(
                    "SELECT * FROM licenses WHERE buyer_email = ? ORDER BY created_ts",
                    (email,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM licenses ORDER BY created_ts"
                ).fetchall()
        return [dict(row) for row in rows]

    def set_status(self, license_id, status):
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE licenses SET status = ? WHERE license_id = ?",
                (status, license_id),
            )
            self._conn.commit()
            return cursor.rowcount > 0

    def set_server_expires(self, license_id, iso):
        with self._lock:
            self._conn.execute(
                "UPDATE licenses SET server_expires_at = ? WHERE license_id = ?",
                (iso, license_id),
            )
            self._conn.commit()

    def machines_used(self, license_id):
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM activations WHERE license_id = ?",
                (license_id,),
            ).fetchone()
        return int(row["n"])

    def record_activation(self, license_id, machine_fp):
        now = self.clock()
        with self._lock:
            self._conn.execute(
                "INSERT INTO activations(license_id, machine_fp, activated_at, last_seen) "
                "VALUES (?,?,?,?) ON CONFLICT(license_id, machine_fp) "
                "DO UPDATE SET last_seen = excluded.last_seen",
                (license_id, machine_fp, now, now),
            )
            self._conn.commit()


# ------------------------------------------------------------ input models --

class ActivateIn(BaseModel):
    key: str
    machine_fp: str


class ValidateIn(BaseModel):
    key: str
    machine_fp: str
    token: str = ""


class IssueIn(BaseModel):
    buyer_email: str = ""
    plan: str = "personal"
    machines_limit: int = None
    days: int = None          # None (and lifetime false) -> lifetime key
    lifetime: bool = False
    features: list = []


class RevokeIn(BaseModel):
    license_id: str = ""
    key: str = ""


PLAN_DEFAULTS = {
    "personal": {"machines": 1, "features": []},
    "pro": {"machines": 2, "features": ["priority_support"]},
}


def _json_env(name):
    try:
        data = json.loads(os.getenv(name, "") or "{}")
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def state_from_env(env=None):
    env = os.environ if env is None else env
    public = licensing.load_public_key(env.get("LICENSE_PUBLIC_KEY", ""))
    private_text = env.get("LICENSE_PRIVATE_KEY", "")
    if not private_text and env.get("LICENSE_PRIVATE_KEY_PATH"):
        try:
            private_text = Path(env["LICENSE_PRIVATE_KEY_PATH"]).read_text(encoding="utf-8")
        except OSError:
            private_text = ""
    private = licensing.load_private_key(private_text)
    if private is not None and public is None:
        public = private.public_key()
    mailer = SmtpMailer.from_env(env)
    fetcher = None
    if env.get("PADDLE_API_KEY"):
        fetcher = paddle_mod.paddle_api_email_fetcher(
            env["PADDLE_API_KEY"], env.get("PADDLE_API_BASE", "https://api.paddle.com")
        )
    return ServerState(
        env.get("LICENSE_SERVER_DB") or str(Path(__file__).resolve().parent / "licenses.db"),
        public_key=public,
        private_key=private,
        admin_token=env.get("LICENSE_ADMIN_TOKEN", ""),
        ls_secret=env.get("LEMONSQUEEZY_WEBHOOK_SECRET", ""),
        ls_variant_map=_json_env("LS_VARIANT_MAP"),
        paddle_secret=env.get("PADDLE_WEBHOOK_SECRET", ""),
        paddle_variant_map=_json_env("PADDLE_VARIANT_MAP"),
        mailer=mailer,
        paddle_email_fetcher=fetcher,
    )


def create_app(state=None, **state_kwargs):
    """Build the FastAPI app. Tests pass a ServerState (or kwargs for one)."""
    if state is None:
        state = ServerState(**state_kwargs) if state_kwargs else state_from_env()
    app = FastAPI(title="CodeHubb Assistant License Server")
    app.state.licensing = state

    # ------------------------------------------------------------- helpers --

    def require_admin(token):
        if not state.admin_token:
            raise HTTPException(503, "admin endpoints are disabled (LICENSE_ADMIN_TOKEN is not set)")
        if not hmac.compare_digest(token or "", state.admin_token):
            raise HTTPException(401, "bad admin token")

    def verified_payload(key_text):
        payload, error = licensing.verify_license(key_text, state.public_key)
        if error:
            raise HTTPException(400, f"invalid license key: {error}")
        return payload

    def issue(buyer_email, plan, days, machines, features, **extra):
        """Sign + store a fresh license. Returns the key string."""
        if state.private_key is None:
            raise HTTPException(503, "this server cannot issue licenses (LICENSE_PRIVATE_KEY is not set)")
        defaults = PLAN_DEFAULTS.get(plan, PLAN_DEFAULTS["personal"])
        payload = licensing.make_payload(
            buyer_email,
            plan=plan,
            machines_limit=machines or defaults["machines"],
            days=days,
            features=features if features else defaults["features"],
            now=state.clock(),
        )
        key = licensing.sign_license(state.private_key, payload)
        state.insert_license(key, payload, **extra)
        return key

    def deliver(key, payload):
        """Email the buyer their key when SMTP is configured; else log it."""
        email = payload.get("buyer_email", "")
        if state.mailer is not None and email:
            try:
                state.mailer.send_license_email(email, key, payload.get("plan", ""))
                return "emailed"
            except Exception as exc:  # noqa: BLE001
                print(f"[license_server] email to {email} failed: {exc}", file=sys.stderr)
                return "email_failed"
        print(
            f"[license_server] email not configured — key for {email or '(no email)'} "
            f"is retrievable via GET /admin/licenses?email={email}",
            file=sys.stderr,
        )
        return "logged"

    def license_state_body(payload):
        row = state.get_license(payload["license_id"])
        expires = payload.get("expires_at")
        machines_used = state.machines_used(payload["license_id"])
        status = "active"
        if row is not None:
            if row["status"] == "revoked":
                status = "revoked"
            if row.get("server_expires_at"):
                expires = row["server_expires_at"]
        expired = (
            licensing._parse_ts(expires) is not None
            and state.clock() > licensing._parse_ts(expires)
        )
        if status == "active" and expired:
            status = "expired"
        body = {
            "state": status,
            "license_id": payload["license_id"],
            "plan": payload["plan"],
            "buyer_email": payload["buyer_email"],
            "expires_at": expires,
            "machines_used": machines_used,
            "machines_limit": payload["machines_limit"],
        }
        if state.private_key is not None:
            body["token"] = licensing.sign_validation_token(
                state.private_key,
                {
                    "license_id": payload["license_id"],
                    "state": status,
                    "exp": state.clock() + TOKEN_TTL_SECONDS,
                },
            )
        return body

    # --------------------------------------------------------- buyer-facing --

    @app.post("/activate")
    def activate(body: ActivateIn):
        payload = verified_payload(body.key)
        row = state.get_license(payload["license_id"])
        if row is None:
            # First time this signed key is seen: the signature IS the
            # authority (seller-issued, e.g. by hand for JazzCash sales).
            state.insert_license(body.key, payload)
            row = state.get_license(payload["license_id"])
        if row["status"] == "revoked":
            raise HTTPException(403, "this license has been revoked")
        expires = licensing._parse_ts(payload.get("expires_at"))
        if expires is not None and state.clock() > expires:
            raise HTTPException(403, "this license has expired")
        used = state.machines_used(payload["license_id"])
        known = state._conn.execute(
            "SELECT 1 FROM activations WHERE license_id = ? AND machine_fp = ?",
            (payload["license_id"], body.machine_fp),
        ).fetchone()
        if not known and used >= payload["machines_limit"]:
            raise HTTPException(
                409,
                f"machine limit reached: this license is already active on "
                f"{used} machine(s) (limit {payload['machines_limit']})",
            )
        state.record_activation(payload["license_id"], body.machine_fp)
        result = license_state_body(payload)
        if result["state"] == "expired":
            raise HTTPException(403, "this license has expired")
        return result

    @app.post("/validate")
    def validate(body: ValidateIn):
        payload = verified_payload(body.key)
        if state.get_license(payload["license_id"]) is None:
            state.insert_license(body.key, payload)
        return license_state_body(payload)

    # ---------------------------------------------------------------- admin --

    @app.post("/admin/licenses")
    def admin_issue(body: IssueIn, x_admin_token: str = Header(default="")):
        require_admin(x_admin_token)
        days = None if body.lifetime else body.days
        key = issue(
            body.buyer_email, body.plan, days, body.machines_limit, body.features
        )
        payload, _ = licensing.verify_license(key, state.public_key)
        return {"key": key, "payload": payload}

    @app.post("/admin/revoke")
    def admin_revoke(body: RevokeIn, x_admin_token: str = Header(default="")):
        require_admin(x_admin_token)
        license_id = body.license_id
        if not license_id and body.key:
            payload, error = licensing.verify_license(body.key, state.public_key)
            if error:
                # Even an unverifiable key's payload segment names the id.
                try:
                    payload = json.loads(licensing._b64d(body.key.split(".")[1]))
                except Exception:  # noqa: BLE001
                    raise HTTPException(400, f"cannot read license id: {error}")
            license_id = payload["license_id"]
        if not license_id or not state.set_status(license_id, "revoked"):
            raise HTTPException(404, "license not found")
        return {"license_id": license_id, "status": "revoked"}

    @app.get("/admin/licenses")
    def admin_list(email: str = "", x_admin_token: str = Header(default="")):
        require_admin(x_admin_token)
        rows = state.list_licenses(email or None)
        return {
            "licenses": [
                {
                    "license_id": row["license_id"],
                    "key": row["key"],
                    "buyer_email": row["buyer_email"],
                    "plan": row["plan"],
                    "status": row["status"],
                    "machines_limit": row["machines_limit"],
                    "machines_used": state.machines_used(row["license_id"]),
                    "server_expires_at": row.get("server_expires_at"),
                }
                for row in rows
            ]
        }

    # ------------------------------------------------------- Lemon Squeezy --

    @app.post("/webhooks/lemonsqueezy")
    async def lemonsqueezy(request: Request):
        raw = await request.body()
        if not state.ls_secret:
            raise HTTPException(503, "Lemon Squeezy webhook is not configured")
        signature = request.headers.get("X-Signature", "")
        expected = hmac.new(
            state.ls_secret.encode("utf-8"), raw, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise HTTPException(401, "bad X-Signature")
        event = json.loads(raw.decode("utf-8") or "{}")
        event_name = (event.get("meta") or {}).get("event_name", "")
        data = event.get("data") or {}
        attributes = data.get("attributes") or {}
        email = attributes.get("user_email", "")
        handled = {"event": event_name, "issued": False}

        if event_name in ("order_created", "subscription_created"):
            variant_id = _ls_variant_id(event_name, data, attributes)
            mapping = state.ls_variant_map.get(str(variant_id), {})
            plan = mapping.get("plan", "personal")
            days = mapping.get("days", LS_PERIOD_DAYS_DEFAULT)
            machines = mapping.get("machines")
            subscription_id = attributes.get("subscription_id") or (
                str(data.get("id")) if event_name == "subscription_created" else None
            )
            if subscription_id and state.find_license(
                ls_subscription_id=str(subscription_id)
            ):
                handled["note"] = "license already issued for this subscription"
                return handled
            key = issue(
                email, plan, days, machines, [],
                ls_subscription_id=str(subscription_id) if subscription_id else None,
                ls_order_id=str(attributes.get("order_id") or data.get("id") or ""),
            )
            payload, _ = licensing.verify_license(key, state.public_key)
            handled["issued"] = True
            handled["license_id"] = payload["license_id"]
            handled["delivery"] = deliver(key, payload)
            return handled

        if event_name == "subscription_payment_success":
            subscription_id = attributes.get("subscription_id")
            row = (
                state.find_license(ls_subscription_id=str(subscription_id))
                if subscription_id else None
            )
            if row is not None:
                days = LS_PERIOD_DAYS_DEFAULT
                mapping = state.ls_variant_map.get(
                    str(attributes.get("variant_id", "")), {}
                )
                days = mapping.get("days", days)
                state.set_server_expires(
                    row["license_id"], licensing._iso(state.clock() + days * 86400)
                )
                handled["extended"] = row["license_id"]
            return handled

        return handled      # other events: acknowledged, nothing to do

    # -------------------------------------------------------------- Paddle --

    @app.post("/webhooks/paddle")
    async def paddle(request: Request):
        raw = await request.body()
        if not state.paddle_secret:
            raise HTTPException(503, "Paddle webhook is not configured")
        header = request.headers.get("Paddle-Signature", "")
        if not paddle_mod.verify_paddle_signature(raw, header, state.paddle_secret):
            raise HTTPException(401, "bad Paddle-Signature")
        event = json.loads(raw.decode("utf-8") or "{}")
        event_type = event.get("event_type", "")
        handled = {"event": event_type, "issued": False}
        if event_type != "transaction.completed":
            return handled
        data = event.get("data") or {}
        transaction_id = str(data.get("id") or "")
        if transaction_id and state.find_license(
            paddle_transaction_id=transaction_id
        ):
            handled["note"] = "license already issued for this transaction"
            return handled
        items = data.get("items") or []
        price_id = str(((items[0] or {}).get("price") or {}).get("id", "")) if items else ""
        mapping = state.paddle_variant_map.get(price_id, {})
        plan = mapping.get("plan", "personal")
        days = mapping.get("days", LS_PERIOD_DAYS_DEFAULT)
        machines = mapping.get("machines")
        customer_id = str(data.get("customer_id") or "")
        email = ""
        if state.paddle_email_fetcher is not None and customer_id:
            try:
                email = state.paddle_email_fetcher(customer_id) or ""
            except Exception as exc:  # noqa: BLE001
                print(f"[license_server] paddle email lookup failed: {exc}", file=sys.stderr)
        key = issue(
            email, plan, days, machines, [],
            paddle_transaction_id=transaction_id or None,
            paddle_customer_id=customer_id or None,
        )
        payload, _ = licensing.verify_license(key, state.public_key)
        handled["issued"] = True
        handled["license_id"] = payload["license_id"]
        handled["delivery"] = deliver(key, payload)
        return handled

    @app.get("/health")
    def health():
        return {"ok": True, "issuing": state.private_key is not None}

    return app


def _ls_variant_id(event_name, data, attributes):
    """Lemon Squeezy payloads differ per event; dig the variant id out."""
    if event_name == "order_created":
        item = attributes.get("first_order_item") or {}
        return item.get("variant_id")
    return attributes.get("variant_id") or data.get("id")


# The uvicorn entry point: `uvicorn license_server.app:app`.
app = create_app()
