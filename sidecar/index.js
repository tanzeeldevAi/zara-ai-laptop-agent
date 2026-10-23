// CodeHubb Assistant — WhatsApp sidecar (Route A, Baileys).
//
// Holds the real WhatsApp connection (paired once by QR scan, like WhatsApp
// Web) and bridges it to the Python agent over a WebSocket on 127.0.0.1.
// This process is deliberately dumb: it owns NO policy. The allowlist, the
// group rule and confirmations all live in Python
// (agent/transport/whatsapp_transport.py). See README.md for the protocol.
//
// Exit codes: 2 = logged out (pairing gone — Python must NOT auto-restart
// us); anything else = crash, Python restarts us with backoff.

import fs from "node:fs";
import path from "node:path";
import { execFile } from "node:child_process";
import { WebSocketServer } from "ws";
import qrcode from "qrcode-terminal";
import pino from "pino";
import makeWASocket, {
  DisconnectReason,
  downloadMediaMessage,
  useMultiFileAuthState,
} from "@whiskeysockets/baileys";

const PORT = parseInt(process.env.WA_BRIDGE_PORT || "17331", 10);
const AUTH_DIR = process.env.WA_AUTH_DIR || path.resolve(".runtime/whatsapp/auth");
const MEDIA_DIR = process.env.WA_MEDIA_DIR || path.resolve(".runtime/whatsapp/media");
const QR_FILE = process.env.WA_QR_FILE || path.resolve(".runtime/whatsapp/qr.txt");
const LOGGED_OUT_EXIT = 2;

fs.mkdirSync(AUTH_DIR, { recursive: true });
fs.mkdirSync(MEDIA_DIR, { recursive: true });
fs.mkdirSync(path.dirname(QR_FILE), { recursive: true });

const logger = pino({ level: "silent" });

// ---------------------------------------------------------------- bridge ---
const clients = new Set();
let waState = { type: "waiting" }; // last state pushed to late-joining clients
let sock = null;
let ownJid = "";

function broadcast(obj) {
  const raw = JSON.stringify(obj);
  for (const ws of clients) {
    if (ws.readyState === ws.OPEN) ws.send(raw);
  }
}

const wss = new WebSocketServer({ host: "127.0.0.1", port: PORT });
wss.on("listening", () => {
  console.log(`[sidecar] bridge listening on ws://127.0.0.1:${PORT}`);
});
wss.on("connection", (ws) => {
  clients.add(ws);
  ws.send(JSON.stringify(waState));
  ws.on("close", () => clients.delete(ws));
  ws.on("error", () => clients.delete(ws));
  ws.on("message", (raw) => {
    let msg;
    try {
      msg = JSON.parse(raw.toString());
    } catch {
      return;
    }
    handleCommand(msg).catch((err) => {
      if (msg.id) {
        ws.send(JSON.stringify({ type: "send_result", id: msg.id, ok: false, error: String(err) }));
      } else {
        console.error("[sidecar] command failed:", err);
      }
    });
  });
});

// -------------------------------------------------------------- sending ----
const MIME_BY_EXT = {
  ".pdf": "application/pdf",
  ".txt": "text/plain",
  ".zip": "application/zip",
  ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  ".mp3": "audio/mpeg",
  ".ogg": "audio/ogg; codecs=opus",
  ".opus": "audio/ogg; codecs=opus",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
};

function convertToOgg(input, output) {
  return new Promise((resolve) => {
    execFile(
      "ffmpeg",
      ["-y", "-i", input, "-c:a", "libopus", "-b:a", "32k", output],
      (err) => resolve(!err && fs.existsSync(output))
    );
  });
}

async function handleCommand(msg) {
  if (!sock) throw new Error("WhatsApp is not connected yet");
  const jid = msg.chatJid;
  const done = (ok, error) => {
    if (msg.id) broadcast({ type: "send_result", id: msg.id, ok, error: error || undefined });
  };
  switch (msg.type) {
    case "send_text":
      await sock.sendMessage(jid, { text: String(msg.text ?? "") });
      return done(true);
    case "send_image": {
      const buf = fs.readFileSync(msg.path);
      await sock.sendMessage(jid, { image: buf, caption: msg.caption || undefined });
      return done(true);
    }
    case "send_file": {
      const buf = fs.readFileSync(msg.path);
      await sock.sendMessage(jid, {
        document: buf,
        fileName: path.basename(msg.path),
        mimetype: MIME_BY_EXT[path.extname(msg.path).toLowerCase()] || "application/octet-stream",
        caption: msg.caption || undefined,
      });
      return done(true);
    }
    case "send_voice": {
      const ext = path.extname(msg.path).toLowerCase();
      let file = msg.path;
      let ptt = ext === ".ogg" || ext === ".opus";
      if (!ptt && ext === ".mp3") {
        const converted = msg.path + ".ogg";
        if (await convertToOgg(msg.path, converted)) {
          file = converted;
          ptt = true;
        }
      }
      const buf = fs.readFileSync(file);
      await sock.sendMessage(jid, {
        audio: buf,
        ptt,
        mimetype: ptt ? "audio/ogg; codecs=opus" : MIME_BY_EXT[ext] || "audio/mpeg",
      });
      return done(true);
    }
    default:
      return done(false, `unknown command type: ${msg.type}`);
  }
}

// -------------------------------------------------------------- receiving --
function jidUser(jid) {
  return jid ? jid.split("@")[0].split(":")[0] : "";
}

function extractText(message) {
  if (!message) return "";
  return (
    message.conversation ||
    message.extendedTextMessage?.text ||
    message.imageMessage?.caption ||
    message.videoMessage?.caption ||
    message.documentMessage?.caption ||
    ""
  );
}

const MEDIA_KINDS = [
  ["audioMessage", "audio", "ogg"],
  ["imageMessage", "image", "jpg"],
  ["documentMessage", "document", "bin"],
  ["videoMessage", "video", "mp4"],
];

async function forwardMessage(msg) {
  const key = msg.key || {};
  const remoteJid = key.remoteJid || "";
  // Never forward: status broadcasts and newsletters/channels.
  if (remoteJid === "status@broadcast" || remoteJid.endsWith("@newsletter")) return;

  const isGroup = remoteJid.endsWith("@g.us");
  const senderJid = isGroup ? key.participant || remoteJid : remoteJid;
  // When WhatsApp routes by LID, the phone JID travels alongside (…Alt / senderPn).
  const phoneJid =
    key.senderPn || key.participantPn || key.remoteJidAlt || key.participantAlt || "";
  const senderNumber = phoneJid
    ? jidUser(phoneJid)
    : remoteJid.endsWith("@s.whatsapp.net")
      ? jidUser(remoteJid)
      : "";

  const out = {
    type: "message",
    id: key.id || "",
    chatJid: remoteJid,
    senderJid,
    senderNumber,
    isGroup,
    fromMe: Boolean(key.fromMe),
    text: extractText(msg.message),
    timestamp: Number(msg.messageTimestamp || 0),
  };

  for (const [field, kind, ext] of MEDIA_KINDS) {
    const mediaMsg = msg.message?.[field];
    if (!mediaMsg) continue;
    try {
      const buffer = await downloadMediaMessage(msg, "buffer", {}, { logger });
      const safeId = (key.id || `m${Date.now()}`).replace(/[^A-Za-z0-9_-]/g, "");
      const fileName = mediaMsg.fileName || `${safeId}.${ext}`;
      const filePath = path.join(MEDIA_DIR, `${safeId}-${fileName}`);
      fs.writeFileSync(filePath, buffer);
      out.media = {
        kind,
        path: filePath,
        mime: mediaMsg.mimetype || "",
        fileName: mediaMsg.fileName || fileName,
      };
    } catch (err) {
      console.error("[sidecar] media download failed:", err?.message || err);
      out.media = { kind, path: "", mime: mediaMsg.mimetype || "", fileName: mediaMsg.fileName || "" };
    }
    break;
  }

  broadcast(out);
}

// ------------------------------------------------------------- connection --
let reconnectDelay = 2000;

async function connect() {
  const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);
  sock = makeWASocket({ auth: state, logger, printQRInTerminal: false });

  sock.ev.on("creds.update", saveCreds);

  sock.ev.on("messages.upsert", async ({ messages, type }) => {
    if (type !== "notify") return;
    for (const msg of messages) {
      try {
        await forwardMessage(msg);
      } catch (err) {
        console.error("[sidecar] forward failed:", err?.message || err);
      }
    }
  });

  sock.ev.on("connection.update", async (update) => {
    const { connection, lastDisconnect, qr } = update;
    if (qr) {
      // Fresh QR for every pairing attempt: console ASCII + file for the wizard.
      try {
        fs.writeFileSync(QR_FILE, qr, "utf8");
      } catch {}
      qrcode.generate(qr, { small: true }, (art) => {
        console.log("[sidecar] scan this QR with WhatsApp → Linked devices:\n" + art);
      });
      waState = { type: "waiting", reason: "pairing" };
      broadcast(waState);
    }
    if (connection === "open") {
      reconnectDelay = 2000;
      ownJid = sock.user?.id || "";
      try {
        fs.rmSync(QR_FILE, { force: true });
      } catch {}
      console.log(`[sidecar] WhatsApp connected as ${ownJid}`);
      waState = { type: "ready", ownJid };
      broadcast(waState);
    }
    if (connection === "close") {
      const statusCode = lastDisconnect?.error?.output?.statusCode;
      const loggedOut = statusCode === DisconnectReason.loggedOut;
      sock = null;
      waState = { type: "waiting", reason: "disconnected" };
      broadcast(waState);
      if (loggedOut) {
        broadcast({ type: "logged_out" });
        console.error("[sidecar] logged out — pairing is gone, exiting with code 2");
        process.exit(LOGGED_OUT_EXIT);
      }
      console.log(`[sidecar] connection closed (code ${statusCode}); reconnecting in ${reconnectDelay}ms`);
      setTimeout(() => connect().catch((e) => console.error("[sidecar] reconnect failed:", e)), reconnectDelay);
      reconnectDelay = Math.min(reconnectDelay * 2, 30000);
    }
  });
}

console.log("[sidecar] starting…");
connect().catch((err) => {
  console.error("[sidecar] fatal start error:", err);
  process.exit(1);
});
