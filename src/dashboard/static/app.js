/* MyBed Agents — small vanilla helpers (theme, dropdowns, toasts, actions). */

// ── Theme ────────────────────────────────────────────────────────────────────
function currentTheme() {
  try { return localStorage.getItem('theme') || 'system' } catch (e) { return 'system' }
}
function applyTheme(t) {
  const dark = t === 'dark' || (t === 'system' && matchMedia('(prefers-color-scheme: dark)').matches)
  document.documentElement.classList.toggle('dark', dark)
  document.querySelectorAll('[data-theme-icon]').forEach((el) => el.classList.toggle('hidden', el.dataset.themeIcon !== t))
  document.dispatchEvent(new CustomEvent('themechange'))
}
function setTheme(t) {
  try { localStorage.setItem('theme', t) } catch (e) {}
  applyTheme(t)
  closeDropdowns()
}
matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => applyTheme(currentTheme()))
applyTheme(currentTheme())

// ── Sidebar drawer (mobile) ──────────────────────────────────────────────────
function toggleSidebar(open) {
  const sb = document.getElementById('sidebar')
  const bd = document.getElementById('sidebar-backdrop')
  sb.classList.toggle('hidden', !open)
  sb.classList.toggle('flex', open)
  sb.classList.toggle('animate-slide-in', open)
  bd.classList.toggle('hidden', !open)
}

// ── Dropdowns ────────────────────────────────────────────────────────────────
function closeDropdowns(except) {
  document.querySelectorAll('[data-dropdown-menu]').forEach((m) => { if (m !== except) m.classList.add('hidden') })
}
document.addEventListener('click', (e) => {
  const trigger = e.target.closest('[data-dropdown-trigger]')
  if (trigger) {
    const menu = trigger.closest('[data-dropdown]').querySelector('[data-dropdown-menu]')
    closeDropdowns(menu)
    menu.classList.toggle('hidden')
    e.stopPropagation()
    return
  }
  if (!e.target.closest('[data-dropdown-menu]')) closeDropdowns()
})
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') { closeDropdowns(); toggleSidebar(false) } })

// ── Toasts ───────────────────────────────────────────────────────────────────
function toast(text, kind = 'info') {
  const box = document.getElementById('toasts')
  const el = document.createElement('div')
  const tone = kind === 'error' ? 'border-red-200 bg-red-50 text-red-700'
    : kind === 'success' ? 'border-emerald-200 bg-emerald-50 text-emerald-700'
    : 'border-line bg-surface text-ink'
  el.className = `pointer-events-auto animate-fade-in rounded-xl border px-3.5 py-2.5 text-sm font-medium shadow-pop max-w-sm ${tone}`
  el.textContent = text
  box.appendChild(el)
  setTimeout(() => { el.style.opacity = '0'; el.style.transition = 'opacity .3s'; setTimeout(() => el.remove(), 300) }, 4200)
}

// ── Requests ─────────────────────────────────────────────────────────────────
async function post(url, body, opts = {}) {
  const init = { method: 'POST', headers: {} }
  if (body instanceof FormData) init.body = body
  else if (body !== undefined) { init.body = JSON.stringify(body); init.headers['Content-Type'] = 'application/json' }
  const res = await fetch(url, init)
  let data = {}
  try { data = await res.json() } catch (e) {}
  if (res.status === 401) { location.href = '/login?next=' + encodeURIComponent(location.pathname); throw new Error('unauthorized') }
  if (!res.ok) throw new Error(data.error || `Błąd ${res.status}`)
  return data
}

function busy(btn, on, label) {
  if (!btn) return
  if (on) {
    btn.dataset.label = btn.innerHTML
    btn.disabled = true
    btn.innerHTML = `<svg class="icon animate-spin"><use href="/static/icons.svg#i-loader-circle"></use></svg> ${label || 'Pracuję…'}`
  } else {
    btn.disabled = false
    if (btn.dataset.label) btn.innerHTML = btn.dataset.label
  }
}

// Generic "run in background" buttons: <button data-run="/api/jobs/x/run" data-msg="...">
document.addEventListener('click', async (e) => {
  const btn = e.target.closest('[data-run]')
  if (!btn) return
  e.preventDefault()
  if (btn.dataset.confirm && !confirm(btn.dataset.confirm)) return
  busy(btn, true, btn.dataset.busy)
  try {
    const fd = new FormData()
    if (btn.dataset.deliver) fd.append('deliver', btn.dataset.deliver)
    await post(btn.dataset.run, fd)
    toast(btn.dataset.msg || 'Uruchomiono w tle', 'success')
    if (btn.dataset.reload) setTimeout(() => location.reload(), Number(btn.dataset.reload))
  } catch (err) {
    toast(err.message, 'error')
  } finally {
    busy(btn, false)
  }
})

// Relative "x min temu" refresh is server-rendered; nothing to do client-side.
