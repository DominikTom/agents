/**
 * WhatsApp Bridge v3 — Baileys 7 (no Chromium), read-only.
 *
 * Links as a WhatsApp "linked device", keeps a local message buffer and
 * exposes it over HTTP to the Python ingestor. Linking can be done from the
 * agents panel: QR code or 8-character pairing code. When WhatsApp logs the
 * device out, the bridge wipes the stale session and generates a new QR by
 * itself — no shell access needed.
 *
 * Auth: every endpoint except GET /health requires the BRIDGE_TOKEN
 * (header `x-bridge-token` or `Authorization: Bearer`). The port must NOT be
 * published outside the Docker network.
 *
 * Endpoints:
 *   GET  /health                      liveness (no auth, no data)
 *   GET  /status                      connection state + QR (data URL) + pairing code
 *   POST /pair     {phone}            request a pairing code for a phone number
 *   POST /restart                     reconnect with the existing session
 *   POST /logout                      unlink device, wipe session, show fresh QR
 *   GET  /chats                       chats known to the buffer
 *   GET  /messages?jid=&since=        messages of one chat newer than `since` (unix s)
 *   GET  /stats                       per-chat buffer statistics
 */

import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import express from 'express'
import pino from 'pino'
import QRCode from 'qrcode'
import makeWASocket, {
  Browsers,
  DisconnectReason,
  fetchLatestBaileysVersion,
  fetchLatestWaWebVersion,
  getContentType,
  isJidBroadcast,
  isJidGroup,
  isJidNewsletter,
  jidNormalizedUser,
  makeCacheableSignalKeyStore,
  normalizeMessageContent,
  useMultiFileAuthState,
} from 'baileys'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const PORT = Number(process.env.PORT || 3001)
const TOKEN = process.env.BRIDGE_TOKEN || ''
const AUTH_DIR = path.join(__dirname, 'auth_info')
const DATA_DIR = path.join(__dirname, 'data')
const STORE_FILE = path.join(DATA_DIR, 'messages.json')
const MAX_PER_CHAT = 2000
const MAX_AGE_DAYS = 120
// Without anyone watching the linking screen we stop rotating QR codes after
// a timeout — otherwise the bridge would reconnect forever while unpaired.
const WATCH_WINDOW_MS = 2 * 60 * 1000

const logger = pino({ level: process.env.LOG_LEVEL || 'warn' })
const log = (...args) => console.log(new Date().toISOString(), '[bridge]', ...args)

// ─── Message buffer ──────────────────────────────────────────────────────────

let messages = {} // jid -> [parsed message]
let chatNames = {} // jid -> group subject / chat name
let contactNames = {} // jid -> contact name (address book / push name)
let dirty = false

function loadStore() {
  try {
    if (!fs.existsSync(STORE_FILE)) return
    const data = JSON.parse(fs.readFileSync(STORE_FILE, 'utf-8'))
    messages = data.messages || {}
    chatNames = data.chatNames || {}
    contactNames = data.contactNames || {}
    const total = Object.values(messages).reduce((n, m) => n + m.length, 0)
    log(`loaded ${total} messages from ${Object.keys(messages).length} chats`)
  } catch (err) {
    log('failed to load store:', err.message)
  }
}

function saveStore() {
  if (!dirty) return
  try {
    fs.mkdirSync(DATA_DIR, { recursive: true })
    const tmp = `${STORE_FILE}.tmp`
    fs.writeFileSync(tmp, JSON.stringify({ messages, chatNames, contactNames }))
    fs.renameSync(tmp, STORE_FILE)
    dirty = false
  } catch (err) {
    log('failed to save store:', err.message)
  }
}

function pruneChat(jid) {
  const cutoff = Math.floor(Date.now() / 1000) - MAX_AGE_DAYS * 86400
  let list = messages[jid].filter((m) => m.timestamp >= cutoff)
  list.sort((a, b) => a.timestamp - b.timestamp)
  if (list.length > MAX_PER_CHAT) list = list.slice(-MAX_PER_CHAT)
  messages[jid] = list
}

function toUnix(ts) {
  if (!ts) return Math.floor(Date.now() / 1000)
  if (typeof ts === 'object') return Number(ts.low ?? ts.toString())
  return Number(ts)
}

function textOf(content) {
  if (!content) return ''
  return (
    content.conversation ||
    content.extendedTextMessage?.text ||
    content.imageMessage?.caption ||
    content.videoMessage?.caption ||
    content.documentMessage?.caption ||
    content.buttonsResponseMessage?.selectedDisplayText ||
    content.listResponseMessage?.title ||
    content.templateButtonReplyMessage?.selectedDisplayText ||
    ''
  )
}

const MEDIA_TYPES = {
  imageMessage: 'image',
  videoMessage: 'video',
  audioMessage: 'audio',
  documentMessage: 'document',
  stickerMessage: 'sticker',
  locationMessage: 'location',
  liveLocationMessage: 'location',
  contactMessage: 'contact',
  contactsArrayMessage: 'contact',
  pollCreationMessage: 'poll',
  pollCreationMessageV2: 'poll',
  pollCreationMessageV3: 'poll',
}

function myJids() {
  const me = sock?.user
  if (!me) return []
  return [me.id, me.lid].filter(Boolean).map((j) => jidNormalizedUser(j))
}

/** Parse a raw Baileys message into the flat shape the ingestor stores. */
function parseMessage(msg) {
  const content = normalizeMessageContent(msg.message)
  if (!content) return null
  const type = getContentType(content)
  if (!type || type === 'senderKeyDistributionMessage' || type === 'reactionMessage') return null

  // Edits and deletions arrive as protocol messages pointing at the original
  if (type === 'protocolMessage') {
    const p = content.protocolMessage
    if (p?.editedMessage && p.key?.id) {
      return { edit: { id: p.key.id, body: textOf(normalizeMessageContent(p.editedMessage)) } }
    }
    if (p?.type === 0 && p.key?.id) return { revoke: { id: p.key.id } }
    return null
  }

  const inner = content[type] || {}
  const ctx = inner.contextInfo || content.extendedTextMessage?.contextInfo || null
  const kind = MEDIA_TYPES[type] || 'text'
  let body = textOf(content)

  if (type === 'documentMessage' && inner.fileName) body = body || inner.fileName
  if (type === 'locationMessage' || type === 'liveLocationMessage') {
    body = [inner.name, inner.address].filter(Boolean).join(', ') || body
  }
  if (type === 'contactMessage') body = inner.displayName || body
  if (type.startsWith('pollCreationMessage')) {
    const opts = (inner.options || []).map((o) => o.optionName).join(' / ')
    body = `${inner.name || 'Ankieta'}${opts ? ` — ${opts}` : ''}`
  }

  const mentions = (ctx?.mentionedJid || []).map((j) => jidNormalizedUser(j))
  const mine = myJids()
  let quoted = null
  if (ctx?.quotedMessage) {
    const q = normalizeMessageContent(ctx.quotedMessage)
    const qType = q ? getContentType(q) : null
    quoted = {
      id: ctx.stanzaId || '',
      from: ctx.participant ? jidNormalizedUser(ctx.participant) : '',
      text: (textOf(q) || (qType ? `[${MEDIA_TYPES[qType] || 'media'}]` : '')).slice(0, 500),
    }
  }

  return {
    id: msg.key?.id || '',
    from: msg.pushName || '',
    participant: msg.key?.participant ? jidNormalizedUser(msg.key.participant) : '',
    fromMe: !!msg.key?.fromMe,
    body,
    type: kind,
    hasMedia: kind !== 'text',
    durationSec: type === 'audioMessage' ? inner.seconds || null : null,
    isVoiceNote: type === 'audioMessage' ? !!inner.ptt : false,
    fileName: type === 'documentMessage' ? inner.fileName || null : null,
    quoted,
    mentions,
    mentionsMe: mentions.some((j) => mine.includes(j)),
    forwarded: !!ctx?.isForwarded,
    timestamp: toUnix(msg.messageTimestamp),
  }
}

function storeMessage(msg) {
  // WhatsApp now addresses some chats by LID; keep phone-number JIDs so history stays in one chat
  const raw = msg.key?.remoteJid
  const jid = raw?.endsWith('@lid') && msg.key?.remoteJidAlt ? jidNormalizedUser(msg.key.remoteJidAlt) : raw
  if (!jid || isJidBroadcast(jid) || isJidNewsletter(jid) || jid === 'status@broadcast') return
  const parsed = parseMessage(msg)
  if (!parsed) return
  const list = (messages[jid] ||= [])

  if (parsed.edit) {
    const target = list.find((m) => m.id === parsed.edit.id)
    if (target && parsed.edit.body) {
      target.body = parsed.edit.body
      target.edited = true
      dirty = true
    }
    return
  }
  if (parsed.revoke) {
    const target = list.find((m) => m.id === parsed.revoke.id)
    if (target) {
      target.revoked = true
      dirty = true
    }
    return
  }
  if (!parsed.body && !parsed.hasMedia) return
  if (parsed.id && list.some((m) => m.id === parsed.id)) return

  // Direct chats: remember the other side's push name as the chat name
  if (!isJidGroup(jid) && !parsed.fromMe && parsed.from && !contactNames[jid]) {
    contactNames[jid] = parsed.from
  }
  list.push(parsed)
  if (list.length > MAX_PER_CHAT + 200) pruneChat(jid)
  dirty = true
  state.lastMessageAt = Math.max(state.lastMessageAt || 0, parsed.timestamp)
}

function chatName(jid) {
  return chatNames[jid] || contactNames[jid] || jid.split('@')[0]
}

// ─── Connection state machine ───────────────────────────────────────────────

let sock = null
let generation = 0
let reconnectTimer = null
let backoffMs = 2000
let groupsRefreshedAt = 0
let preQrFailures = 0 // closes before any QR on an unregistered session

// WA_BROWSER=ubuntu|macos|windows (default ubuntu Chrome — Baileys' own default)
function browserConfig() {
  switch ((process.env.WA_BROWSER || 'ubuntu').toLowerCase()) {
    case 'macos':
      return Browsers.macOS('Desktop')
    case 'windows':
      return Browsers.windows('Desktop')
    default:
      return Browsers.ubuntu('Chrome')
  }
}

const state = {
  status: 'starting', // starting | waiting_qr | connecting | connected | reconnecting | idle | logged_out
  qr: null,
  qrDataUrl: null,
  qrAt: null,
  pairingCode: null,
  pairingPhone: null,
  me: null,
  connectedAt: null,
  lastDisconnect: null,
  lastMessageAt: null,
  lastStatusPollAt: 0,
  history: { batches: 0, messages: 0, lastAt: null, done: false },
  waVersion: null,
}

const REASONS = {
  401: 'Urządzenie zostało wylogowane z WhatsApp',
  403: 'WhatsApp odrzucił połączenie',
  408: 'Przekroczono czas oczekiwania',
  411: 'Niezgodność multi-device',
  428: 'Połączenie zamknięte',
  440: 'Sesja zastąpiona przez inne połączenie',
  500: 'Uszkodzona sesja',
  503: 'WhatsApp chwilowo niedostępny',
  515: 'Wymagany restart po sparowaniu',
}

function wipeAuth() {
  // auth_info is a Docker volume mount point — delete its contents, not the directory
  fs.mkdirSync(AUTH_DIR, { recursive: true })
  for (const name of fs.readdirSync(AUTH_DIR)) {
    try {
      fs.rmSync(path.join(AUTH_DIR, name), { recursive: true, force: true })
    } catch (err) {
      log('failed to remove', name, err.message)
    }
  }
}

function closeSocket() {
  if (!sock) return
  try {
    sock.ev.removeAllListeners()
    sock.end(undefined)
  } catch {
    /* already closed */
  }
  sock = null
}

function schedule(fn, ms) {
  clearTimeout(reconnectTimer)
  reconnectTimer = setTimeout(fn, ms)
}

function someoneWatching() {
  return Date.now() - state.lastStatusPollAt < WATCH_WINDOW_MS
}

async function refreshGroups(force = false) {
  if (!sock || state.status !== 'connected') return
  if (!force && Date.now() - groupsRefreshedAt < 60 * 60 * 1000) return
  try {
    const groups = await sock.groupFetchAllParticipating()
    for (const [id, meta] of Object.entries(groups)) {
      if (meta.subject) chatNames[id] = meta.subject
    }
    groupsRefreshedAt = Date.now()
    dirty = true
  } catch (err) {
    log('group refresh failed:', err.message)
  }
}

async function start() {
  clearTimeout(reconnectTimer)
  closeSocket()
  const gen = ++generation
  const { state: auth, saveCreds } = await useMultiFileAuthState(AUTH_DIR)
  // WhatsApp closes the socket for clients reporting an outdated web version,
  // so prefer the live version from web.whatsapp.com (override: WA_VERSION=2.3000.x)
  let version
  if (process.env.WA_VERSION) {
    version = process.env.WA_VERSION.split('.').map(Number)
  } else {
    const web = await fetchLatestWaWebVersion({ timeout: 10000 }).catch(() => ({}))
    if (web?.isLatest) version = web.version
    else {
      const lib = await fetchLatestBaileysVersion().catch(() => ({}))
      version = lib?.version
    }
  }
  if (gen !== generation) return
  state.waVersion = version ? version.join('.') : 'bundled'
  log(`starting (WA ${state.waVersion}, registered: ${!!auth.creds.registered})`)
  state.status = auth.creds.registered ? 'connecting' : 'starting'

  let sawQr = false
  sock = makeWASocket({
    version,
    auth: { creds: auth.creds, keys: makeCacheableSignalKeyStore(auth.keys, logger) },
    logger,
    browser: browserConfig(),
    printQRInTerminal: false,
    syncFullHistory: process.env.WA_FULL_HISTORY === '1',
    connectTimeoutMs: 30000,
    markOnlineOnConnect: false, // don't steal notifications from the phone
    generateHighQualityLinkPreview: false,
  })
  const s = sock

  s.ev.on('creds.update', saveCreds)

  s.ev.on('connection.update', async (update) => {
    if (gen !== generation) return
    const { connection, lastDisconnect, qr } = update

    if (qr) {
      if (!sawQr) log('QR ready — scan it in the panel')
      sawQr = true
      preQrFailures = 0
      state.status = 'waiting_qr'
      state.qr = qr
      state.qrAt = Date.now()
      try {
        state.qrDataUrl = await QRCode.toDataURL(qr, { margin: 1, width: 320 })
      } catch (err) {
        log('qr render failed:', err.message)
      }
    }

    if (connection === 'open') {
      backoffMs = 2000
      state.status = 'connected'
      state.qr = state.qrDataUrl = state.pairingCode = state.pairingPhone = null
      state.connectedAt = Date.now()
      const id = s.user?.id ? jidNormalizedUser(s.user.id) : null
      state.me = id ? { id, name: s.user?.name || s.user?.notify || '', phone: id.split('@')[0] } : null
      log(`connected as ${state.me?.phone || '?'}`)
      refreshGroups(true)
    }

    if (connection === 'close') {
      const code = lastDisconnect?.error?.output?.statusCode
      const detail = lastDisconnect?.error?.message || ''
      state.lastDisconnect = {
        code: code ?? null,
        reason: [REASONS[code], detail].filter(Boolean).join(' — ') || 'Rozłączono',
        at: Date.now(),
      }
      state.connectedAt = null
      log(`connection closed (${code ?? 'no code'}): ${state.lastDisconnect.reason}`,
        lastDisconnect?.error?.data ? JSON.stringify(lastDisconnect.error.data).slice(0, 300) : '')
      if (process.env.LOG_LEVEL === 'debug' && lastDisconnect?.error?.stack) log(lastDisconnect.error.stack)

      if (code === DisconnectReason.loggedOut || code === DisconnectReason.badSession || code === DisconnectReason.forbidden) {
        // Session is dead for good — start over with a fresh QR
        state.status = 'logged_out'
        state.me = null
        wipeAuth()
        if (someoneWatching()) schedule(start, 1500)
        else state.status = 'idle'
        return
      }
      if (code === DisconnectReason.restartRequired) {
        schedule(start, 500)
        return
      }
      const registered = !!s.authState?.creds?.registered
      if (!registered && !sawQr) {
        // Closed before WhatsApp even sent a QR: after a few tries start from fresh keys,
        // and back off hard so WhatsApp doesn't throttle this IP
        preQrFailures += 1
        if (preQrFailures % 3 === 0) {
          log(`no QR after ${preQrFailures} attempts — wiping session keys`)
          wipeAuth()
        }
      }
      if (!registered && !someoneWatching()) {
        // QR rotation ended and nobody is looking — wait for the panel
        state.status = 'idle'
        state.qr = state.qrDataUrl = null
        return
      }
      state.status = 'reconnecting'
      let delay = code === DisconnectReason.connectionReplaced ? 30000 : backoffMs
      if (!registered && !sawQr) delay = Math.max(delay, 15000)
      backoffMs = Math.min(backoffMs * 2, 60000)
      schedule(start, delay)
    }
  })

  s.ev.on('messages.upsert', ({ messages: batch }) => {
    for (const m of batch) storeMessage(m)
  })

  s.ev.on('messaging-history.set', ({ messages: batch, chats, contacts, isLatest }) => {
    for (const c of chats || []) if (c.id && c.name) chatNames[c.id] = c.name
    for (const c of contacts || []) {
      const name = c.name || c.notify || c.verifiedName
      if (c.id && name) contactNames[c.id] = name
    }
    for (const m of batch || []) storeMessage(m)
    state.history.batches += 1
    state.history.messages += (batch || []).length
    state.history.lastAt = Date.now()
    if (isLatest) state.history.done = true
    dirty = true
    log(`history batch: ${(batch || []).length} messages, ${(chats || []).length} chats`)
  })

  const onContacts = (list) => {
    for (const c of list || []) {
      const name = c.name || c.notify || c.verifiedName
      if (c.id && name) {
        contactNames[c.id] = name
        dirty = true
      }
    }
  }
  s.ev.on('contacts.upsert', onContacts)
  s.ev.on('contacts.update', onContacts)
  s.ev.on('chats.upsert', (chats) => {
    for (const c of chats || []) if (c.id && c.name) chatNames[c.id] = c.name
  })
  s.ev.on('groups.upsert', (groups) => {
    for (const g of groups || []) if (g.id && g.subject) chatNames[g.id] = g.subject
  })
  s.ev.on('groups.update', (groups) => {
    for (const g of groups || []) if (g.id && g.subject) chatNames[g.id] = g.subject
  })
}

// ─── HTTP API ────────────────────────────────────────────────────────────────

const app = express()
app.use(express.json({ limit: '64kb' }))

app.get('/health', (req, res) => {
  res.json({ ok: true, status: state.status })
})

app.use((req, res, next) => {
  if (!TOKEN) return next()
  const header = req.get('x-bridge-token') || (req.get('authorization') || '').replace(/^Bearer\s+/i, '')
  if (header !== TOKEN) return res.status(401).json({ error: 'unauthorized' })
  next()
})

function publicState() {
  const total = Object.values(messages).reduce((n, m) => n + m.length, 0)
  return {
    status: state.status,
    connected: state.status === 'connected',
    qr: state.status === 'waiting_qr' ? state.qrDataUrl : null,
    qrAt: state.qrAt,
    pairingCode: state.pairingCode,
    pairingPhone: state.pairingPhone,
    me: state.me,
    connectedAt: state.connectedAt,
    lastDisconnect: state.lastDisconnect,
    lastMessageAt: state.lastMessageAt,
    history: state.history,
    store: { chats: Object.keys(messages).length, messages: total },
    waVersion: state.waVersion,
  }
}

app.get('/status', (req, res) => {
  state.lastStatusPollAt = Date.now()
  // Someone opened the linking screen — resume QR rotation if it was paused
  if (state.status === 'idle') {
    state.status = 'starting'
    start().catch((err) => log('start failed:', err.message))
  }
  res.json(publicState())
})

app.post('/pair', async (req, res) => {
  state.lastStatusPollAt = Date.now()
  const phone = String(req.body?.phone || '').replace(/\D/g, '')
  if (phone.length < 9) return res.status(400).json({ error: 'Podaj numer z kierunkowym, np. 48600100200' })
  if (!sock || sock.authState?.creds?.registered) {
    return res.status(409).json({ error: 'Urządzenie jest już sparowane — najpierw wyloguj' })
  }
  if (state.status !== 'waiting_qr') {
    return res.status(409).json({ error: 'Połączenie jeszcze się uruchamia — spróbuj za kilka sekund' })
  }
  try {
    const code = await sock.requestPairingCode(phone)
    state.pairingCode = code?.match(/.{1,4}/g)?.join('-') || code
    state.pairingPhone = phone
    res.json({ pairingCode: state.pairingCode })
  } catch (err) {
    res.status(500).json({ error: err.message })
  }
})

app.post('/restart', async (req, res) => {
  state.lastStatusPollAt = Date.now()
  state.status = 'starting'
  start().catch((err) => log('restart failed:', err.message))
  res.json({ ok: true })
})

app.post('/logout', async (req, res) => {
  state.lastStatusPollAt = Date.now()
  const old = sock
  const wasConnected = state.status === 'connected'
  generation++ // ignore close events of the old socket
  try {
    if (old && wasConnected) await old.logout()
  } catch (err) {
    log('logout call failed (continuing):', err.message)
  }
  closeSocket()
  wipeAuth()
  state.me = null
  state.status = 'starting'
  start().catch((err) => log('start after logout failed:', err.message))
  res.json({ ok: true })
})

app.get('/chats', async (req, res) => {
  await refreshGroups()
  const known = new Set([...Object.keys(messages), ...Object.keys(chatNames).filter((j) => isJidGroup(j))])
  const chats = [...known]
    .filter((jid) => !isJidBroadcast(jid) && !isJidNewsletter(jid))
    .map((jid) => {
      const list = messages[jid] || []
      return {
        id: jid,
        name: chatName(jid),
        isGroup: !!isJidGroup(jid),
        messageCount: list.length,
        lastMessageAt: list.length ? list[list.length - 1].timestamp : null,
      }
    })
  res.json({ chats, connected: state.status === 'connected' })
})

app.get('/messages', (req, res) => {
  const since = Number(req.query.since || 0)
  let jid = req.query.jid
  // Legacy lookup by name (v2 API) — exact match first, then substring
  if (!jid && (req.query.chat || req.query.contact)) {
    const term = String(req.query.chat || req.query.contact).toLowerCase()
    const all = Object.keys(messages)
    jid = all.find((j) => chatName(j).toLowerCase() === term) || all.find((j) => chatName(j).toLowerCase().includes(term))
  }
  const list = (jid && messages[jid]) || []
  const out = list.filter((m) => m.timestamp >= since)
  res.json({ jid: jid || null, chat: jid ? chatName(jid) : null, isGroup: jid ? !!isJidGroup(jid) : false, messages: out, total: out.length })
})

app.get('/stats', (req, res) => {
  const stats = {}
  for (const [jid, list] of Object.entries(messages)) {
    stats[jid] = {
      name: chatName(jid),
      messageCount: list.length,
      oldest: list[0]?.timestamp ?? null,
      newest: list[list.length - 1]?.timestamp ?? null,
    }
  }
  res.json(stats)
})

// ─── Boot ────────────────────────────────────────────────────────────────────

fs.mkdirSync(AUTH_DIR, { recursive: true })
fs.mkdirSync(DATA_DIR, { recursive: true })
loadStore()
for (const jid of Object.keys(messages)) pruneChat(jid)
setInterval(saveStore, 15000)
for (const sig of ['SIGTERM', 'SIGINT']) {
  process.on(sig, () => {
    dirty = true
    saveStore()
    process.exit(0)
  })
}

if (!TOKEN) log('WARNING: BRIDGE_TOKEN not set — API is unauthenticated')
app.listen(PORT, () => {
  log(`listening on :${PORT}`)
  start().catch((err) => {
    log('initial start failed:', err.message)
    state.status = 'reconnecting'
    schedule(start, 5000)
  })
})
