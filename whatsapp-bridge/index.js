/**
 * WhatsApp Web Bridge - Read-only HTTP API for fetching WhatsApp messages.
 *
 * Endpoints:
 *   GET /health              - Check if client is connected
 *   GET /chats               - List all chats
 *   GET /messages?chat=NAME&since=TIMESTAMP  - Get messages from a chat
 *   GET /messages?contact=NAME&since=TIMESTAMP - Get messages from a contact
 *
 * On first run, scan the QR code in terminal to authenticate.
 * Session is persisted in .wwebjs_auth/ directory.
 */

const { Client, LocalAuth } = require("whatsapp-web.js");
const qrcode = require("qrcode-terminal");
const express = require("express");

const app = express();
const PORT = process.env.PORT || 3001;

const client = new Client({
  authStrategy: new LocalAuth(),
  puppeteer: {
    headless: true,
    args: ["--no-sandbox", "--disable-setuid-sandbox"],
  },
});

let isReady = false;

client.on("qr", (qr) => {
  console.log("Scan this QR code to authenticate:");
  qrcode.generate(qr, { small: true });
});

client.on("ready", () => {
  console.log("WhatsApp client is ready!");
  isReady = true;
});

client.on("disconnected", (reason) => {
  console.log("Client disconnected:", reason);
  isReady = false;
});

// Health check
app.get("/health", (req, res) => {
  res.json({ status: isReady ? "connected" : "disconnected" });
});

// List chats
app.get("/chats", async (req, res) => {
  if (!isReady) return res.status(503).json({ error: "Client not ready" });

  try {
    const chats = await client.getChats();
    const chatList = chats.map((chat) => ({
      id: chat.id._serialized,
      name: chat.name,
      isGroup: chat.isGroup,
      unreadCount: chat.unreadCount,
    }));
    res.json({ chats: chatList });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// Get messages
app.get("/messages", async (req, res) => {
  if (!isReady) return res.status(503).json({ error: "Client not ready" });

  const { chat, contact, since } = req.query;
  const sinceTs = since ? parseInt(since) : 0;

  try {
    let targetChat = null;

    if (chat) {
      // Find chat by name (group or individual)
      const chats = await client.getChats();
      targetChat = chats.find(
        (c) => c.name && c.name.toLowerCase().includes(chat.toLowerCase())
      );
    } else if (contact) {
      // Find 1:1 chat by contact name
      const chats = await client.getChats();
      targetChat = chats.find(
        (c) =>
          !c.isGroup &&
          c.name &&
          c.name.toLowerCase().includes(contact.toLowerCase())
      );
    }

    if (!targetChat) {
      return res.json({ messages: [], note: "Chat not found" });
    }

    const messages = await targetChat.fetchMessages({ limit: 50 });

    const filtered = messages
      .filter((msg) => msg.timestamp >= sinceTs)
      .map((msg) => ({
        from: msg.author || msg.from,
        body: msg.body,
        timestamp: msg.timestamp,
        hasMedia: msg.hasMedia,
        type: msg.type,
      }));

    res.json({ messages: filtered });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// Start
client.initialize();

app.listen(PORT, () => {
  console.log(`WhatsApp bridge running on port ${PORT}`);
});
