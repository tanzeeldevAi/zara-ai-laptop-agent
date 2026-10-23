# WhatsApp sidecar (Route A)

A small Node.js bridge that holds the actual WhatsApp connection for the
CodeHubb Assistant's WhatsApp transport, using
[@whiskeysockets/baileys](https://github.com/WhiskeySockets/Baileys) — the
"WhatsApp Web" protocol. It pairs once by QR scan and then reconnects on its
own. **This is an unofficial route**: use a dedicated secondary number, never
your main business number (see the disclaimer in `.env.example`).

## Who owns what

The sidecar is deliberately dumb. It owns **no policy**:

- It forwards every 1:1 and group message it receives (it drops only status
  broadcasts and newsletters/channels, which can never be the owner).
- The **allowlist, the group-ignore rule and the YES/NO confirmations all
  live in Python** (`agent/transport/whatsapp_transport.py`). A message from
  a stranger is dropped by Python the moment it crosses the bridge.

## Bridge design — why the sidecar is the server

The sidecar runs a WebSocket **server** on `127.0.0.1:$WA_BRIDGE_PORT`
(default 17331) and the Python transport connects to it as a client:

- Python spawns and supervises this process, so the connection direction
  follows the lifecycle direction: Python waits/retries until the port is
  up, and reconnects if the bridge drops.
- The WhatsApp session survives bridge reconnects — if Python restarts, it
  re-attaches to a sidecar that never lost the WhatsApp connection, and the
  sidecar immediately replays its current state (`ready`/`waiting`).

## Protocol (JSON, one object per WebSocket message)

Sidecar → Python:

```json
{"type": "ready", "ownJid": "92300…@s.whatsapp.net"}
{"type": "waiting", "reason": "pairing | disconnected"}
{"type": "logged_out"}
{"type": "message", "id": "…", "chatJid": "…", "senderJid": "…",
 "senderNumber": "92300…", "isGroup": false, "fromMe": false,
 "text": "…", "media": {"kind": "audio|image|document|video",
 "path": ".runtime/whatsapp/media/…", "mime": "…", "fileName": "…"},
 "timestamp": 0}
{"type": "send_result", "id": "…", "ok": true}
```

`senderNumber` is the sender's phone digits when WhatsApp provides them —
including when the chat is routed by LID (`@lid`), where the phone JID
arrives in the message key (`senderPn`/`remoteJidAlt`). Self-chat messages
(`fromMe: true`, the primary way the owner talks to the agent) are forwarded
like any other.

Python → sidecar:

```json
{"type": "send_text",  "chatJid": "…", "text": "…"}
{"type": "send_file",  "chatJid": "…", "path": "…", "kind": "document", "caption": "…"}
{"type": "send_image", "chatJid": "…", "path": "…", "caption": "…"}
{"type": "send_voice", "chatJid": "…", "path": "…"}
```

Voice: `.ogg`/`.opus` files are sent as voice notes (`ptt`). An `.mp3` is
converted to Opus with `ffmpeg` when it is available; otherwise it is sent
as a plain audio message. An optional `"id"` on any command gets a
`send_result` reply.

## Pairing & state

- On start with no saved auth, the sidecar prints an ASCII QR in its console
  **and** writes the raw QR string to `.runtime/whatsapp/qr.txt` (fresh on
  every attempt) so a future setup wizard can render it. Scan it from
  WhatsApp → Settings → Linked devices.
- Auth state persists in `.runtime/whatsapp/auth` (Baileys multi-file auth
  state); downloaded media lands in `.runtime/whatsapp/media`. `.runtime/`
  is gitignored — the auth folder is effectively a login credential, treat
  it like one.
- Reconnects use exponential backoff (2s → 30s). If WhatsApp logs the device
  out, the sidecar exits with **code 2** and does not retry: Python stops
  restarting it until the owner deletes `.runtime/whatsapp/auth` and
  re-pairs. Any other exit is a crash and Python restarts it with backoff.

## Run it standalone (for debugging)

```bash
cd sidecar && npm install
node index.js          # bridge on ws://127.0.0.1:17331
```

Env: `WA_BRIDGE_PORT`, `WA_AUTH_DIR`, `WA_MEDIA_DIR`, `WA_QR_FILE`.
(without any WhatsApp account it simply sits in the "waiting for pairing" state)
