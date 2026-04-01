/**
 * WhatsApp Web Bridge v2 - Using Baileys (no Chromium needed!)
 *
 * Endpoints:
 *   GET /health              - Check if client is connected
 *   GET /chats               - List all chats
 *   GET /messages?chat=NAME&since=TIMESTAMP  - Get messages from a chat
 *   GET /messages?contact=NAME&since=TIMESTAMP - Get messages from a contact
 *
 * On first run, scan the QR code in terminal to authenticate.
 * Session is persisted in ./auth_info/ directory.
 */

const {
  default: makeWASocket,
  useMultiFileAuthState,
  DisconnectReason,
} = require("@whiskeysockets/baileys");
const pino = require("pino");
const qrcode = require("qrcode-terminal");
const express = require("express");
const path = require("path");

const app = express();
const PORT = process.env.PORT || 3001;
const AUTH_DIR = path.join(__dirname, "auth_info");

let sock = null;
let isReady = false;

async function startWhatsApp() {
  const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);

  sock = makeWASocket({
    auth: state,
    logger: pino({ level: "silent" }),
    printQRInTerminal: false,
  });

  // Handle QR code
  sock.ev.on("connection.update", (update) => {
    const { connection, lastDisconnect, qr } = update;

    if (qr) {
      console.log("Scan this QR code to authenticate:");
      qrcode.generate(qr, { small: true });
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
        setTimeout(startWhatsApp, 3000);
      } else {
        console.log("Logged out. Delete auth_info/ and restart to re-authenticate.");
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
    // Get all chats from the store
    const chats = await sock.groupFetchAllParticipating();
    const chatList = Object.entries(chats).map(([id, meta]) => ({
      id,
      name: meta.subject || id,
      isGroup: true,
      participants: meta.participants?.length || 0,
    }));

    // Also list some recent individual chats from contacts
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
  const sinceTs = since ? parseInt(since) * 1000 : 0; // Convert to ms

  try {
    let jid = null;

    if (chat) {
      // Find group by name
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
      // For individual contacts, we need to find their JID
      // This is a simplified approach - search by name in contacts
      const contacts = sock.store?.contacts || {};
      for (const [id, c] of Object.entries(contacts)) {
        if (
          c.name &&
          c.name.toLowerCase().includes(contact.toLowerCase())
        ) {
          jid = id;
          break;
        }
      }
      // If not found in contacts, try direct number format
      if (!jid && /^\+?\d+$/.test(contact)) {
        jid = contact.replace("+", "") + "@s.whatsapp.net";
      }
    }

    if (!jid) {
      return res.json({ messages: [], note: "Chat not found" });
    }

    // Fetch messages using Baileys store
    // Note: Baileys doesn't persist message history by default,
    // it only gets messages received while connected
    const messages = sock.store?.messages?.[jid]?.array || [];

    const filtered = messages
      .filter((msg) => {
        const msgTs = (msg.messageTimestamp || 0) * 1000;
        return msgTs >= sinceTs;
      })
      .map((msg) => ({
        from: msg.pushName || msg.key.participant || msg.key.remoteJid,
        body:
          msg.message?.conversation ||
          msg.message?.extendedTextMessage?.text ||
          msg.message?.imageMessage?.caption ||
          "(media)",
        timestamp: msg.messageTimestamp,
        hasMedia: !!(
          msg.message?.imageMessage ||
          msg.message?.videoMessage ||
          msg.message?.documentMessage
        ),
        type: msg.key.fromMe ? "sent" : "received",
      }));

    res.json({ messages: filtered });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// Start
startWhatsApp().catch(console.error);

app.listen(PORT, () => {
  console.log(`WhatsApp bridge running on port ${PORT}`);
});
