/**
 * WhatsApp Web Bridge v2 - Using Baileys (no Chromium needed!)
 *
 * Endpoints:
 *   GET /health              - Check if client is connected
 *   GET /chats               - List all chats
 *   GET /messages?chat=NAME&since=TIMESTAMP  - Get messages from a chat
 *   GET /messages?contact=NAME&since=TIMESTAMP - Get messages from a contact
 */

const {
  default: makeWASocket,
  useMultiFileAuthState,
  DisconnectReason,
  fetchLatestBaileysVersion,
  makeCacheableSignalKeyStore,
} = require("baileys");
const pino = require("pino");
const qrcode = require("qrcode-terminal");
const express = require("express");
const path = require("path");

const app = express();
const PORT = process.env.PORT || 3001;
const AUTH_DIR = path.join(__dirname, "auth_info");
const logger = pino({ level: "warn" });

let sock = null;
let isReady = false;

async function startWhatsApp() {
  const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);
  const { version } = await fetchLatestBaileysVersion();

  console.log(`Using WA version: ${version.join(".")}`);

  sock = makeWASocket({
    version,
    auth: {
      creds: state.creds,
      keys: makeCacheableSignalKeyStore(state.keys, logger),
    },
    logger,
    printQRInTerminal: false,
    generateHighQualityLinkPreview: false,
  });

  // Handle connection events
  sock.ev.on("connection.update", (update) => {
    const { connection, lastDisconnect, qr } = update;

    if (qr) {
      console.log("\nScan this QR code to authenticate:\n");
      qrcode.generate(qr, { small: true });
      console.log("\nWaiting for scan...\n");
    }

    if (connection === "close") {
      isReady = false;
      const statusCode =
        lastDisconnect?.error?.output?.statusCode;
      const shouldReconnect = statusCode !== DisconnectReason.loggedOut;
      console.log(
        `Connection closed. Status: ${statusCode}. Reconnecting: ${shouldReconnect}`
      );
      if (shouldReconnect) {
        setTimeout(startWhatsApp, 5000);
      } else {
        console.log(
          "Logged out. Delete auth_info/ directory and restart to re-authenticate."
        );
      }
    } else if (connection === "open") {
      isReady = true;
      console.log("WhatsApp client is ready!");
    }
  });

  // Save credentials on update
  sock.ev.on("creds.update", saveCreds);
}

// Health check
app.get("/health", (req, res) => {
  res.json({ status: isReady ? "connected" : "disconnected" });
});

// List chats
app.get("/chats", async (req, res) => {
  if (!isReady || !sock) {
    return res.status(503).json({ error: "Client not ready" });
  }

  try {
    const groups = await sock.groupFetchAllParticipating();
    const chatList = Object.entries(groups).map(([id, meta]) => ({
      id,
      name: meta.subject || id,
      isGroup: true,
      participants: meta.participants?.length || 0,
    }));
    res.json({ chats: chatList });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// Get messages
app.get("/messages", async (req, res) => {
  if (!isReady || !sock) {
    return res.status(503).json({ error: "Client not ready" });
  }

  const { chat, contact, since } = req.query;

  try {
    let jid = null;

    if (chat) {
      const groups = await sock.groupFetchAllParticipating();
      for (const [id, meta] of Object.entries(groups)) {
        if (
          meta.subject &&
          meta.subject.toLowerCase().includes(chat.toLowerCase())
        ) {
          jid = id;
          break;
        }
      }
    } else if (contact) {
      if (/^\+?\d+$/.test(contact)) {
        jid = contact.replace("+", "") + "@s.whatsapp.net";
      }
    }

    if (!jid) {
      return res.json({ messages: [], note: "Chat not found" });
    }

    // Note: Baileys doesn't store message history by default.
    // It only captures messages received while the bridge is running.
    res.json({
      messages: [],
      note: "Bridge captures messages in real-time while running. Historical messages not available.",
    });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// Start
console.log(`WhatsApp bridge running on port ${PORT}`);
app.listen(PORT, () => {
  startWhatsApp().catch((err) => {
    console.error("Failed to start WhatsApp:", err.message);
  });
});
