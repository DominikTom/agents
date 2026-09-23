// Connectivity check: docker compose exec whatsapp-bridge node diag.mjs
import dns from 'node:dns/promises'

const host = 'web.whatsapp.com'
const out = (...a) => console.log(...a)

for (const family of [4, 6]) {
  try {
    const addrs = await dns.lookup(host, { family, all: true })
    out(`DNS IPv${family}:`, addrs.map((a) => a.address).join(', ') || '-')
  } catch (err) {
    out(`DNS IPv${family}: ERR ${err.code || err.message}`)
  }
}

try {
  const r = await fetch(`https://${host}`, { signal: AbortSignal.timeout(10000) })
  out('HTTPS:', r.status)
} catch (err) {
  out('HTTPS: ERR', err.cause?.code || err.message)
}

await new Promise((resolve) => {
  const started = Date.now()
  const ws = new WebSocket(`wss://${host}/ws/chat`, { headers: { Origin: 'https://web.whatsapp.com' } })
  ws.binaryType = 'arraybuffer'
  const timer = setTimeout(() => {
    out('WebSocket: no answer in 15 s')
    ws.close()
    resolve()
  }, 15000)
  ws.onopen = () => out(`WebSocket: open after ${Date.now() - started} ms`)
  ws.onerror = (e) => out('WebSocket: ERR', e.message || e.error?.message || 'error')
  ws.onclose = (e) => {
    clearTimeout(timer)
    out(`WebSocket: closed code=${e.code} after ${Date.now() - started} ms`)
    resolve()
  }
})
