# CodeHubb Assistant — License Server

The small server the seller hosts. Buyers' apps activate and re-validate
their signed license keys against it; Lemon Squeezy / Paddle payments are
turned into license keys by its webhooks; the seller can also issue and
revoke keys through its admin endpoints (the manual JazzCash/bank route).

It is a plain FastAPI app with a SQLite database — any small VPS or cheap
Python hosting runs it. It holds the seller's Ed25519 **private** key:
that key signs new licenses and validation tokens, so guard the host like
you'd guard a payment account.

## Run it

```bash
# from the REPOSITORY ROOT (the server imports the agent package)
pip install -r license_server/requirements.txt

# 1. one-time: create the seller keypair (private key OUTSIDE the repo)
python tools/issue_license.py --init --private-key-out ~/seller-keys/license_private.pem

# 2. configure the environment (see below), then start
uvicorn license_server.app:app --host 0.0.0.0 --port 8787
```

Put a reverse proxy with HTTPS in front (Caddy/nginx) — license keys and
admin tokens travel in these requests. Buyers' apps point at it with
`LICENSE_SERVER_URL=https://license.your-domain.example` in their `.env`.

## Environment variables

| Variable | Required | Purpose |
|---|---|---|
| `LICENSE_PRIVATE_KEY_PATH` / `LICENSE_PRIVATE_KEY` | for issuing | Seller private key (file path, or PEM/base64 text). Without it the server still verifies/activates keys but cannot issue new ones or sign tokens. |
| `LICENSE_PUBLIC_KEY` | with private key absent | Public key for verification when the private key lives elsewhere. If the private key is set, the public key is derived from it. |
| `LICENSE_ADMIN_TOKEN` | for admin endpoints | Long random string; sent as the `X-Admin-Token` header. Admin endpoints are disabled when unset. |
| `LICENSE_SERVER_DB` | no | SQLite path (default: `license_server/licenses.db`). |
| `LEMONSQUEEZY_WEBHOOK_SECRET` | for LS | The signing secret of the Lemon Squeezy webhook (store settings → Webhooks). |
| `LS_VARIANT_MAP` | no | JSON: variant id → plan/days/machines, e.g. `{"12345":{"plan":"personal","days":35,"machines":1},"23456":{"plan":"pro","days":35,"machines":2}}`. Unmapped variants default to personal / 35 days / plan machines. |
| `PADDLE_WEBHOOK_SECRET` | for Paddle | The notification destination's secret key (`pdl_ntfset_…`). |
| `PADDLE_VARIANT_MAP` | no | JSON: price id → plan/days/machines, same shape as `LS_VARIANT_MAP`. |
| `PADDLE_API_KEY` | no | Lets the server resolve a Paddle `customer_id` to the buyer's email. Without it, Paddle licenses are issued with an empty email (still retrievable via the admin list). |
| `PADDLE_API_BASE` | no | `https://api.paddle.com` (live) or `https://sandbox-api.paddle.com`. |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM`, `SMTP_TLS` | no | When `SMTP_HOST` is set, issued keys are emailed to buyers. When it is not, the server logs that email is off and the key stays retrievable via `GET /admin/licenses?email=`. |

## Lemon Squeezy setup

1. Create the products/variants in Lemon Squeezy (one variant per plan).
2. In LS → Settings → Webhooks, add a webhook pointing at
   `https://<your-host>/webhooks/lemonsqueezy` with the events
   `order_created`, `subscription_created` and
   `subscription_payment_success`, and copy its signing secret into
   `LEMONSQUEEZY_WEBHOOK_SECRET`.
3. Use LS checkout links/overlays on your product page — LS is the
   merchant of record (it handles cards, tax/VAT). On payment, the
   webhook issues a signed license for the buyer's email and (with SMTP
   configured) emails it. Each `subscription_payment_success` extends the
   license's server-side expiry by the mapped period, so a subscription
   that keeps paying keeps working.

## Paddle setup

Same shape: create a notification destination pointing at
`/webhooks/paddle` for `transaction.completed`, put its secret key in
`PADDLE_WEBHOOK_SECRET`, and map price ids in `PADDLE_VARIANT_MAP`.
**Read the certainty note at the top of `license_server/paddle.py`** —
the signature scheme is implemented from Paddle's public docs and must be
confirmed once against a live (sandbox) Paddle dashboard before you rely
on it for real sales.

## Manual sales (JazzCash / bank transfer)

No server endpoints needed beyond activation: mint a key locally with
`tools/issue_license.py --email … --plan … --days …|--lifetime`, send it
to the buyer however you like (WhatsApp/email). The buyer's app verifies
it offline by signature; the first `/activate` call registers their
machine against the limit. Revoke with `tools/revoke_license.py` (it
calls this server's admin API, or prints the SQL when no server is
configured).

## Endpoints

| Method & path | Auth | Purpose |
|---|---|---|
| `POST /activate` | signed key | First activation; enforces the machine limit; returns a signed 30-day validation token + state. |
| `POST /validate` | signed key | Current state incl. revocation and server-side expiry. |
| `POST /webhooks/lemonsqueezy` | `X-Signature` HMAC | Payments → licenses. |
| `POST /webhooks/paddle` | `Paddle-Signature` HMAC | Payments → licenses. |
| `POST /admin/licenses` | `X-Admin-Token` | Issue a key by hand. |
| `POST /admin/revoke` | `X-Admin-Token` | Revoke by `license_id` or full key. |
| `GET /admin/licenses?email=` | `X-Admin-Token` | List licenses incl. keys (support retrieval). |
| `GET /health` | — | Liveness + whether issuing is configured. |
