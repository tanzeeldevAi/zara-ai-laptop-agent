"""Paddle Billing webhook adapter.

Implements Paddle's current notification signature scheme:

    header:  Paddle-Signature: ts=<unix>;h1=<hex>
    signed:  HMAC-SHA256(notification_secret, f"{ts}:" + raw_body)  (hex)

and maps `transaction.completed` to "issue a license".

WHAT MUST BE CONFIRMED against a live Paddle dashboard before relying on
this in production (the scheme above is implemented from Paddle's public
docs, and the test vectors in tests/test_license_server.py are computed
against THIS implementation, not against Paddle):

  1. The exact signature header format and signed-payload construction
     for the current Paddle Billing notification destinations.
  2. Whether a timestamp tolerance should be enforced (Paddle's docs have
     suggested one; verify_paddle_signature here only checks the HMAC,
     optionally with a caller-supplied max age).
  3. The transaction payload fields used below (items[0].price.id,
     customer_id) and the event name `transaction.completed`.
  4. Buyer email: transaction webhooks carry a customer_id, NOT an email.
     The default fetcher resolves it via GET {PADDLE_API_BASE}/customers/
     {id} with the PADDLE_API_KEY bearer token — confirm the API base for
     sandbox (https://sandbox-api.paddle.com) vs live
     (https://api.paddle.com) and that the key has customer read scope.
     Without an API key, licenses are issued with an empty email and stay
     retrievable via GET /admin/licenses (search by paddle id in the log).
"""

import hashlib
import hmac
import json
import time
import urllib.request


def parse_signature_header(header):
    """'ts=123;h1=abc;h1=def' -> (ts str, [h1, ...])."""
    ts, signatures = "", []
    for part in str(header or "").split(";"):
        part = part.strip()
        if part.startswith("ts="):
            ts = part[3:]
        elif part.startswith("h1="):
            signatures.append(part[3:])
    return ts, signatures


def verify_paddle_signature(raw_body, header, secret, *, now=None, max_age=None):
    """True when any h1 in the header matches HMAC(secret, ts:raw_body)."""
    if not secret or not header:
        return False
    if isinstance(raw_body, str):
        raw_body = raw_body.encode("utf-8")
    ts, signatures = parse_signature_header(header)
    if not ts or not signatures:
        return False
    if max_age is not None:
        try:
            age = abs((now or time.time()) - int(ts))
        except (TypeError, ValueError):
            return False
        if age > max_age:
            return False
    signed = ts.encode("utf-8") + b":" + raw_body
    expected = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, sig) for sig in signatures)


def paddle_api_email_fetcher(api_key, api_base="https://api.paddle.com"):
    """Build the default customer_id -> email resolver (Paddle API)."""

    def fetch(customer_id):
        if not api_key or not customer_id:
            return ""
        request = urllib.request.Request(
            f"{api_base.rstrip('/')}/customers/{customer_id}",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            data = json.loads(response.read().decode("utf-8") or "{}")
        return (data.get("data") or {}).get("email") or ""

    return fetch
