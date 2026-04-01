/**
 * WhatsApp Web Bridge v2 - Using Baileys (no Chromium needed!)
 *
 * Endpoints:
 *   GET /health              - Check if client is connected
 *   GET /chats               - List all chats
 *   GET /messages?chat=NAME&since=TIMESTAMP  - Get messages from a group chat
 *   GET /messages?contact=NAME&since=TIMESTAMP - Get messages from a contact
 *   GET /stats               - Message store statistics
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
const fs = require("fs");

const app = express();
const PORT = process.env.PORT || 3001;
const AUTH_DIR = path.join(__dirname, "auth_info");
const MESSAGES_FILE = path.join(__dirname, "data", "messages.json");
const logger = pino({ level: "warn" });

let sock = null;
let isReady = false;

// --- Message Store ---
// Stores messages keyed by chat JID
let messageStore = {};
let chatNames = {}; // JID -> chat name mapping

function ensureDataDir() {
  const dataDir = path.join(__dirname, "data");
  if (!fs.existsSync(dataDir)) {
    fs.mkdirSync(dataDir, { recursive: true });
  }
}

function loadMessages() {
  try {
    if (fs.existsSync(MESSAGES_FILE)) {
      const data = JSON.parse(fs.readFileSync(MESSAGES_FILE, "utf-8"));
      messageStore = data.messages || {};
      chatNames = data.chatNames || {};
      const totalMsgs = Object.values(messageStore).reduce(
        (sum, msgs) => sum + msgs.length,
        0
      );
      console.log(
        `Loaded ${totalMsgs} messages from ${Object.keys(messageStore).length} chats`
      );
    }
  } catch (err) {
    console.error("Failed to load messages:", err.message);
    messageStore = {};
    chatNames = {};
  }
}

function saveMessages() {
  try {
    ensureDataDir();
    fs.writeFileSync(
      MESSAGES_FILE,
      JSON.stringify({ messages: messageStore, chatNames }, null, 0)
    );
  } catch (err) {
    console.error("Failed to save messages:", err.message);
  }
}

// Save periodically (every 30 seconds)
setInterval(saveMessages, 30000);

function storeMessage(jid, msg) {
  if (!messageStore[jid]) {
    messageStore[jid] = [];
  }

  const parsed = {
    id: msg.key?.id || "",
    from: msg.pushName || msg.key?.participant || msg.key?.remoteJid || "",
    fromMe: msg.key?.fromMe || false,
    body:
      msg.message?.conversation ||
      msg.message?.extendedTextMessage?.text ||
      msg.message?.imageMessage?.caption ||
      msg.message?.videoMessage?.caption ||
      msg.message?.documentMessage?.caption ||
      msg.message?.listResponseMessage?.title ||
      "",
    timestamp: msg.messageTimestamp
      ? typeof msg.messageTimestamp === "object"
        ? parseInt(msg.messageTimestamp.low || msg.messageTimestamp)
        : parseInt(msg.messageTimestamp)
      : Math.floor(Date.now() / 1000),
    hasMedia: !!(
      msg.message?.imageMessage ||
      msg.message?.videoMessage ||
      msg.message?.documentMessage ||
      msg.message?.audioMessage
    ),
    type: msg.key?.fromMe ? "sent" : "received",
  };

  // Skip empty messages and duplicates
  if (!parsed.body && !parsed.hasMedia) return;
  if (messageStore[jid].some((m) => m.id === parsed.id && parsed.id)) return;

  messageStore[jid].push(parsed);

  // Keep max 500 messages per chat
  if (messageStore[jid].length > 500) {
    messageStore[jid] = messageStore[jid].slice(-500);
  }
}

// --- WhatsApp Connection ---

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
    syncFullHistory: true,
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
      const statusCode = lastDisconnect?.error?.output?.statusCode;
      const shouldReconnect = statusCode !== DisconnectReason.loggedOut;
      console.log(
        `Connection closed. Status: ${statusCode}. Reconnecting: ${shouldReconnect}`
      );
      if (shouldReconnect) {
        setTimeout(startWhatsApp, 5000);
      } else {
        console.log("Logged out. Delete auth_info/ and restart.");
      }
    } else if (connection === "open") {
      isReady = true;
      console.log("WhatsApp client is ready!");
    }
  });

  // Save credentials
  sock.ev.on("creds.update", saveCreds);

  // --- Capture real-time messages ---
  sock.ev.on("messages.upsert", ({ messages: msgs, type }) => {
    for (const msg of msgs) {
      const jid = msg.key.remoteJid;
      if (!jid || jid === "status@broadcast") continue;
      storeMessage(jid, msg);
    }
    console.log(
      `[messages.upsert] ${msgs.length} messages (type: ${type})`
    );
  });

  // --- Capture history sync (this is where old messages come from!) ---
  sock.ev.on("messaging-history.set", ({ messages: msgs, chats, isLatest }) => {
    console.log(
      `[history-sync] Received ${msgs.length} messages, ${chats.length} chats (isLatest: ${isLatest})`
    );

    // Store chat names
    for (const chat of chats) {
      if (chat.id && chat.name) {
        chatNames[chat.id] = chat.name;
      }
    }

    // Store all history messages
    for (const msg of msgs) {
      const jid = msg.key?.remoteJid;
      if (!jid || jid === "status@broadcast") continue;
      storeMessage(jid, msg);
    }

    // Save after history sync
    saveMessages();

    const totalMsgs = Object.values(messageStore).reduce(
      (sum, m) => sum + m.length,
      0
    );
    console.log(
      `[history-sync] Total stored: ${totalMsgs} messages in ${Object.keys(messageStore).length} chats`
    );
  });

  // Capture chat updates for names
  sock.ev.on("chats.upsert", (chats) => {
    for (const chat of chats) {
      if (chat.id && chat.name) {
        chatNames[chat.id] = chat.name;
      }
    }
  });

  sock.ev.on("groups.upsert", (groups) => {
    for (const group of groups) {
      if (group.id && group.subject) {
        chatNames[group.id] = group.subject;
      }
    }
  });
}

// --- API Endpoints ---

// Health check
app.get("/health", (req, res) => {
  const totalMsgs = Object.values(messageStore).reduce(
    (sum, m) => sum + m.length,
    0
  );
  res.json({
    status: isReady ? "connected" : "disconnected",
    chats: Object.keys(messageStore).length,
    messages: totalMsgs,
  });
});

// Stats
app.get("/stats", (req, res) => {
  const stats = {};
  for (const [jid, msgs] of Object.entries(messageStore)) {
    const name = chatNames[jid] || jid;
    stats[name] = {
      jid,
      messageCount: msgs.length,
      oldest: msgs.length > 0 ? msgs[0].timestamp : null,
      newest: msgs.length > 0 ? msgs[msgs.length - 1].timestamp : null,
    };
  }
  res.json(stats);
});

// List chats
app.get("/chats", async (req, res) => {
  if (!isReady || !sock) {
    // Even if not connected, return stored chat data
    const chatList = Object.entries(messageStore).map(([jid, msgs]) => ({
      id: jid,
      name: chatNames[jid] || jid,
      isGroup: jid.endsWith("@g.us"),
      messageCount: msgs.length,
    }));
    return res.json({ chats: chatList, fromStore: true });
  }

  try {
    const groups = await sock.groupFetchAllParticipating();

    // Merge with stored data
    for (const [id, meta] of Object.entries(groups)) {
      if (meta.subject) {
        chatNames[id] = meta.subject;
      }
    }

    const chatList = Object.entries(messageStore).map(([jid, msgs]) => ({
      id: jid,
      name: chatNames[jid] || jid,
      isGroup: jid.endsWith("@g.us"),
      messageCount: msgs.length,
    }));

    // Add groups without messages
    for (const [id, meta] of Object.entries(groups)) {
      if (!messageStore[id]) {
        chatList.push({
          id,
          name: meta.subject || id,
          isGroup: true,
          messageCount: 0,
        });
      }
    }

    res.json({ chats: chatList });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// Get messages from a chat
app.get("/messages", async (req, res) => {
  const { chat, contact, since } = req.query;
  const sinceTs = since ? parseInt(since) : 0;

  try {
    let matchedJid = null;
    const searchTerm = (chat || contact || "").toLowerCase();

    // Search by name in chatNames and messageStore
    for (const [jid, name] of Object.entries(chatNames)) {
      if (name.toLowerCase().includes(searchTerm)) {
        matchedJid = jid;
        break;
      }
    }

    // Also try matching JID directly
    if (!matchedJid) {
      for (const jid of Object.keys(messageStore)) {
        if (jid.toLowerCase().includes(searchTerm)) {
          matchedJid = jid;
          break;
        }
      }
    }

    if (!matchedJid || !messageStore[matchedJid]) {
      return res.json({ messages: [], note: `Chat "${searchTerm}" not found` });
    }

    const messages = messageStore[matchedJid].filter(
      (msg) => msg.timestamp >= sinceTs
    );

    res.json({
      chat: chatNames[matchedJid] || matchedJid,
      jid: matchedJid,
      messages,
      total: messages.length,
    });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// --- Start ---

ensureDataDir();
loadMessages();

console.log(`WhatsApp bridge running on port ${PORT}`);
app.listen(PORT, () => {
  startWhatsApp().catch((err) => {
    console.error("Failed to start WhatsApp:", err.message);
  });
});
