"""Agents panel — FastAPI + Jinja2, styled with the MyBed Group OS design system."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from urllib.parse import quote, urlparse

import yaml
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from src.common.config import CONFIG_DIR
from src.common.timeutil import ago_pl, fmt_date_pl, fmt_short, hours_since, now, to_local, today
from src.dashboard import data
from src.dashboard.auth import COOKIE, SESSION_DAYS, password_configured, verify_login, verify_session
from src.mcp_server.auth import BearerAuthMiddleware
from src.mcp_server.oauth import router as oauth_router
from src.mcp_server.server import create_mcp_app, set_mcp_db
from src.storage.database import Database, _j

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

_mcp_asgi = create_mcp_app()
_db: Database | None = None
_background: set[asyncio.Task] = set()


def get_db_sync() -> Database:
    if _db is None:
        raise RuntimeError("Database not initialized")
    return _db


@asynccontextmanager
async def lifespan(app):
    global _db
    _db = Database()
    await _db.init()
    set_mcp_db(_db)
    async with _mcp_asgi.router.lifespan_context(_mcp_asgi):
        yield
    await _db.close()


app = FastAPI(title="MyBed Agents", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(oauth_router)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.mount("/mcp", BearerAuthMiddleware(_mcp_asgi))
templates = Jinja2Templates(directory=TEMPLATES_DIR)


# ─── Template helpers ────────────────────────────────────────────────────────


def _pln(v, digits: int = 0) -> str:
    if v is None:
        return "—"
    s = f"{float(v):,.{digits}f}".replace(",", " ").replace(".", ",")
    return f"{s} zł"


def _num(v) -> str:
    if v is None:
        return "—"
    return f"{float(v):,.0f}".replace(",", " ")


def _pct(v) -> str:
    if v is None:
        return ""
    return f"{'+' if v > 0 else ''}{v:.0f}%".replace(".", ",")


def _time(dt) -> str:
    dt = to_local(dt)
    return dt.strftime("%H:%M") if dt else ""


templates.env.filters.update({
    "ago": ago_pl,
    "short": fmt_short,
    "pln": _pln,
    "num": _num,
    "pct": _pct,
    "hm": _time,
    "hours": lambda dt: round(hours_since(dt)),
    "datepl": fmt_date_pl,
    "j": lambda v: _j(v, {}),
    "avatar_key": lambda s: sum(ord(c) for c in (s or "")) % 9,
})
templates.env.globals.update({"now": now, "today": today})

NAV = [
    ("Start", [
        ("/", "Pulpit", "layout-dashboard", "pulpit"),
        ("/reports", "Raporty", "file-text", "reports"),
        ("/studio", "Studio raportów", "sliders-horizontal", "studio"),
    ]),
    ("Komunikacja", [
        ("/chats", "Rozmowy", "message-circle", "chats"),
        ("/commitments", "Zobowiązania", "square-check", "commitments"),
        ("/topics", "Wątki", "waypoints", "topics"),
    ]),
    ("System", [
        ("/sources", "Źródła danych", "plug", "sources"),
        ("/people", "Ludzie", "users", "people"),
        ("/system", "System", "activity", "system"),
    ]),
]


def render(request: Request, template: str, active: str = "", **ctx):
    return templates.TemplateResponse(request, template, {"nav": NAV, "active": active, **ctx})


def authed(request: Request) -> bool:
    return verify_session(request.cookies.get(COOKIE))


def login_redirect(request: Request) -> RedirectResponse:
    return RedirectResponse(f"/login?next={quote(str(request.url.path))}", status_code=302)


def unauthorized() -> JSONResponse:
    return JSONResponse({"error": "unauthorized"}, status_code=401)


def spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)


def _safe_next(request: Request, next_url: str | None) -> str:
    """Only redirect within this site (relative path, or our own host)."""
    if not next_url:
        return "/"
    parsed = urlparse(next_url)
    if parsed.netloc:
        return next_url if parsed.netloc == request.url.netloc and parsed.scheme in ("http", "https") else "/"
    return next_url if next_url.startswith("/") and not next_url.startswith("//") else "/"


# ─── Auth ────────────────────────────────────────────────────────────────────


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str | None = None):
    return render(request, "login.html", error=None, next=next or "", configured=password_configured())


@app.post("/login")
async def login_submit(request: Request, username: str = Form(...), password: str = Form(...), next: str = Form("")):
    token = verify_login(username, password)
    if not token:
        await asyncio.sleep(1)  # slow down guessing
        return render(request, "login.html", error="Nieprawidłowy login lub hasło", next=next,
                      configured=password_configured())
    response = RedirectResponse(_safe_next(request, next), status_code=302)
    secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    response.set_cookie(COOKIE, token, httponly=True, secure=secure, samesite="lax", max_age=SESSION_DAYS * 86400)
    return response


@app.get("/logout")
async def logout_route():
    response = RedirectResponse("/login", status_code=302)
    response.delete_cookie(COOKIE)
    return response


# ─── Pulpit ──────────────────────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
async def pulpit_page(request: Request):
    if not authed(request):
        return login_redirect(request)
    ctx = await data.pulpit(get_db_sync())
    series = (ctx["dash"] or {}).get("series", [])
    return render(request, "pulpit.html", "pulpit", **ctx, series_json=json.dumps(series))


# ─── Reports ─────────────────────────────────────────────────────────────────

REPORT_KEYS = {"morning_briefing": "Poranny briefing", "daily_wrap": "Podsumowanie dnia", "weekly_review": "Podsumowanie tygodnia"}


@app.get("/reports", response_class=HTMLResponse)
async def reports_page(request: Request, kind: str | None = None, page: int = 1):
    if not authed(request):
        return login_redirect(request)
    db = get_db_sync()
    per_page = 20
    kind = kind if kind in REPORT_KEYS else None
    reports = await db.get_reports(agent_name=kind, limit=per_page, offset=(page - 1) * per_page)
    total = await db.get_reports_count(agent_name=kind)
    for r in reports:
        r["meta_d"] = data.parse_meta(r)
    runs = await db.get_last_run_any(list(REPORT_KEYS))
    return render(request, "reports.html", "reports", reports=reports, kind=kind, page=page,
                  pages=max(1, (total + per_page - 1) // per_page), labels=REPORT_KEYS, runs=runs)


@app.get("/reports/{report_id}", response_class=HTMLResponse)
async def report_detail_page(request: Request, report_id: int):
    if not authed(request):
        return login_redirect(request)
    db = get_db_sync()
    report = await db.get_report(report_id)
    if not report:
        return RedirectResponse("/reports", status_code=302)
    if not report.get("html"):
        from src.reports.render import to_html
        report["html"] = to_html(report["body"])
    from src.reports.profiles import SECTION_CATALOG

    return render(request, "report_detail.html", "reports", report=report, meta=data.parse_meta(report),
                  labels=REPORT_KEYS, catalog=SECTION_CATALOG)


@app.post("/api/reports/{key}/run")
async def run_report_now(request: Request, key: str, deliver: str = Form("0")):
    if not authed(request):
        return unauthorized()
    if key not in REPORT_KEYS:
        return JSONResponse({"error": "unknown report"}, status_code=404)
    from src.reports.engine import ReportEngine

    engine = ReportEngine(get_db_sync())

    async def go():
        try:
            await engine.run(key, deliver=deliver == "1")
        except Exception:
            pass
    spawn(go())
    return {"status": "started"}


@app.post("/api/reports/{report_id}/resend")
async def resend_report(request: Request, report_id: int):
    if not authed(request):
        return unauthorized()
    db = get_db_sync()
    report = await db.get_report(report_id)
    if not report:
        return JSONResponse({"error": "not found"}, status_code=404)
    from src.reports.engine import ReportEngine
    from src.reports.profiles import load_general, load_profiles

    profiles = await load_profiles(db)
    profile = profiles.get(report["agent_name"]) or next(iter(profiles.values()))
    profile = {**profile, "email": True, "slack_channel": ""}
    delivered = await ReportEngine(db).deliver(
        report_id, profile, await load_general(db), report.get("title") or "Raport",
        report.get("summary") or "", report["body"], report.get("html") or "",
    )
    if "email_error" in delivered:
        return JSONResponse({"error": delivered["email_error"]}, status_code=400)
    return {"status": "sent", "to": delivered.get("email", {}).get("to")}


# ─── Studio ──────────────────────────────────────────────────────────────────


@app.get("/studio", response_class=HTMLResponse)
async def studio_root(request: Request):
    return RedirectResponse("/studio/morning_briefing", status_code=302)


@app.get("/studio/{key}", response_class=HTMLResponse)
async def studio_page(request: Request, key: str, saved: int = 0):
    if not authed(request):
        return login_redirect(request)
    from src.ai.client import MODEL_CHOICES
    from src.reports.profiles import DAY_NAMES, LENGTHS, SECTION_CATALOG, load_general, load_profiles, schedule_label

    db = get_db_sync()
    profiles = await load_profiles(db)
    general = await load_general(db)
    if key != "general" and key not in profiles:
        return RedirectResponse("/studio/morning_briefing", status_code=302)
    last = await db.get_latest_report(key) if key in profiles else None
    return render(
        request, "studio.html", "studio",
        key=key, profiles=profiles, profile=profiles.get(key), general=general, catalog=SECTION_CATALOG,
        models=MODEL_CHOICES, lengths=LENGTHS, day_names=DAY_NAMES, schedule_label=schedule_label,
        saved=saved, last=last, config=data.config_status(),
    )


def _profile_from_form(form, base: dict) -> dict:
    p = dict(base)
    p["enabled"] = form.get("enabled") == "on"
    p["time"] = (form.get("time") or base.get("time") or "07:00")[:5]
    p["days"] = sorted({int(d) for d in form.getlist("days") if d.isdigit() and 0 <= int(d) <= 6})
    p["email"] = form.get("email") == "on"
    p["slack_channel"] = (form.get("slack_channel") or "").strip()
    p["model"] = form.get("model") or base.get("model")
    p["length"] = form.get("length") if form.get("length") in ("short", "standard", "detailed") else "standard"
    p["instructions"] = (form.get("instructions") or "").strip()[:4000]
    p["name"] = (form.get("name") or base.get("name")).strip()[:80]
    recipients = [r.strip() for r in (form.get("recipients") or "").replace(";", ",").split(",") if "@" in r]
    p["recipients"] = recipients
    order = (form.get("order") or "").split(",")
    sections = []
    for k in order:
        if not k:
            continue
        sections.append({"key": k, "enabled": form.get(f"sec_{k}") == "on", "note": (form.get(f"note_{k}") or "").strip()[:500]})
    if sections:
        p["sections"] = sections
    return p


@app.post("/studio/{key}")
async def studio_save(request: Request, key: str):
    if not authed(request):
        return login_redirect(request)
    from src.reports.profiles import load_general, load_profiles

    db = get_db_sync()
    form = await request.form()
    if key == "general":
        general = await load_general(db)
        general["about"] = (form.get("about") or "").strip()[:4000]
        general["vip"] = [v.strip() for v in (form.get("vip") or "").split("\n") if v.strip()][:30]
        general["recipients"] = [r.strip() for r in (form.get("recipients") or "").replace(";", ",").split(",") if "@" in r]
        general["push_to_os_suggestions"] = form.get("push_to_os_suggestions") == "on"
        await db.set_setting("general", general)
        return RedirectResponse("/studio/general?saved=1", status_code=302)
    profiles = await load_profiles(db)
    if key not in profiles:
        return RedirectResponse("/studio", status_code=302)
    profiles[key] = _profile_from_form(form, profiles[key])
    await db.set_setting("reports", profiles)
    return RedirectResponse(f"/studio/{key}?saved=1", status_code=302)


@app.post("/api/studio/{key}/preview")
async def studio_preview(request: Request, key: str):
    """Generate a report from the (unsaved) form state and return it — nothing is sent."""
    if not authed(request):
        return unauthorized()
    from src.reports.engine import ReportEngine
    from src.reports.profiles import load_profiles

    db = get_db_sync()
    profiles = await load_profiles(db)
    if key not in profiles:
        return JSONResponse({"error": "unknown report"}, status_code=404)
    form = await request.form()
    profile = _profile_from_form(form, profiles[key])
    try:
        result = await ReportEngine(db).run(key, deliver=False, preview=True, profile_override=profile)
    except Exception as e:
        return JSONResponse({"error": str(e)[:300]}, status_code=500)
    return {"id": result["id"], "title": result["title"], "lead": result["lead"], "html": result["html"],
            "errors": result["errors"]}


# ─── Chats ───────────────────────────────────────────────────────────────────


@app.get("/chats", response_class=HTMLResponse)
@app.get("/chats/{jid}", response_class=HTMLResponse)
async def chats_page(request: Request, jid: str | None = None, filter: str = "active", day: str | None = None):
    if not authed(request):
        return login_redirect(request)
    db = get_db_sync()
    configs = await db.get_whatsapp_chat_configs()
    waiting = {w["jid"]: w for w in await db.get_whatsapp_awaiting(days=5, limit=100)}
    digests_today = {d["chat_jid"]: d for d in await db.get_chat_digests(today() - timedelta(days=1), limit=300)}
    for c in configs:
        c["waiting"] = waiting.get(c["jid"])
        c["digest"] = digests_today.get(c["jid"])
        # Bridge names direct chats after the address book; fall back to the sender's push name only for bare numbers
        name = c["chat_name"] or ""
        c["display"] = c.get("contact_name") if (not c["is_group"] and name.lstrip("+").isdigit() and c.get("contact_name")) else name
    counts = {
        "active": sum(1 for c in configs if c["enabled"] is not False and c["total_messages"]),
        "waiting": sum(1 for c in configs if c["waiting"] and c["enabled"] is not False),
        "new": sum(1 for c in configs if c["enabled"] is None and c["total_messages"]),
        "off": sum(1 for c in configs if c["enabled"] is False),
    }
    if filter == "waiting":
        shown = [c for c in configs if c["waiting"] and c["enabled"] is not False]
    elif filter == "new":
        shown = [c for c in configs if c["enabled"] is None and c["total_messages"]]
    elif filter == "off":
        shown = [c for c in configs if c["enabled"] is False]
    else:
        filter = "active"
        shown = [c for c in configs if c["enabled"] is not False and c["total_messages"]]

    selected = messages = digests = None
    if jid:
        selected = next((c for c in configs if c["jid"] == jid), None)
        if selected:
            messages = await db.get_chat_messages(jid, since=now() - timedelta(days=14), limit=250)
            digests = await db.get_chat_digests(today() - timedelta(days=14), jid=jid, limit=14)
    return render(request, "chats.html", "chats", chats=shown, counts=counts, filter=filter,
                  selected=selected, messages=messages, digests=digests,
                  bridge=await data.bridge_status())


@app.post("/api/whatsapp/chat/toggle")
async def toggle_whatsapp_chat(request: Request, jid: str = Form(...), enabled: str = Form(...), back: str = Form("")):
    if not authed(request):
        return unauthorized()
    await get_db_sync().set_whatsapp_chat_enabled(jid, enabled == "true")
    return RedirectResponse(back if back.startswith("/") else "/chats", status_code=302)


@app.post("/api/chats/digest")
async def run_digests(request: Request):
    if not authed(request):
        return unauthorized()
    from src import jobs

    spawn(jobs.chat_digests(get_db_sync()))
    return {"status": "started"}


# ─── Commitments ─────────────────────────────────────────────────────────────


@app.get("/commitments", response_class=HTMLResponse)
async def commitments_page(request: Request, status: str = "open"):
    if not authed(request):
        return login_redirect(request)
    db = get_db_sync()
    status = status if status in ("open", "done", "dismissed") else "open"
    rows = await db.list_commitments(status=status, limit=300)
    from src.reports.profiles import load_general

    general = await load_general(db)
    return render(request, "commitments.html", "commitments", rows=rows, status=status,
                  counts=await db.count_commitments(), os_configured=data.config_status()["os"],
                  push_suggestions=general.get("push_to_os_suggestions"))


@app.post("/api/commitments/{cid}/status")
async def commitment_status(request: Request, cid: int, status: str = Form(...)):
    if not authed(request):
        return unauthorized()
    if status not in ("open", "done", "dismissed"):
        return JSONResponse({"error": "bad status"}, status_code=400)
    await get_db_sync().set_commitment_status(cid, status)
    return {"status": status}


@app.post("/api/commitments/{cid}/os")
async def commitment_to_os(request: Request, cid: int, mode: str = Form("task")):
    """Approved by the CEO: create a private OS task (or, if enabled, a team-visible AI suggestion)."""
    if not authed(request):
        return unauthorized()
    from src.connectors.os_mybed import OSClient
    from src.reports.profiles import load_general

    db = get_db_sync()
    c = await db.get_commitment(cid)
    if not c:
        return JSONResponse({"error": "not found"}, status_code=404)
    client = OSClient()
    if not client.configured:
        return JSONResponse({"error": "MyBed OS nie jest skonfigurowany (OS_SUPABASE_SERVICE_KEY)"}, status_code=400)
    source = {"whatsapp": "WhatsApp", "gmail": "Gmail"}.get(c["source"], c["source"])
    when = fmt_short(c.get("source_at"))
    description = "\n".join(filter(None, [
        f"Źródło: {source} — {c.get('chat_name') or ''} ({when})",
        f"Z kim: {c['counterpart']}" if c.get("counterpart") else "",
        f"Cytat: „{c['context']}”" if c.get("context") else "",
        f"Termin z rozmowy: {c['due_hint']}" if c.get("due_hint") else "",
        "Dodane z panelu MyBed Agents.",
    ]))
    try:
        if mode == "suggestion":
            general = await load_general(db)
            if not general.get("push_to_os_suggestions"):
                return JSONResponse({"error": "Propozycje AI w OS są wyłączone w Studio → Ogólne"}, status_code=400)
            ref = await client.create_suggestion(c["title"], description, f"Z rozmowy: {c.get('chat_name') or source}", c["source"])
        else:
            ref = await client.create_private_task(c["title"], description, c.get("due_date"))
    except Exception as e:
        return JSONResponse({"error": str(e)[:300]}, status_code=502)
    await db.mark_commitment_pushed(cid, ref)
    data.invalidate("os")
    return {"status": "created", "ref": ref, "link": client.link("task", ref) if mode != "suggestion" else None}


# ─── Topics ──────────────────────────────────────────────────────────────────


@app.get("/topics", response_class=HTMLResponse)
async def topics_page(request: Request):
    if not authed(request):
        return login_redirect(request)
    db = get_db_sync()
    topics = []
    for t in await db.get_active_topics(limit=40):
        full = await db.get_topic_with_events(t["id"], limit=20)
        if full:
            full["event_count"] = t.get("event_count", 0)
            topics.append(full)
    return render(request, "topics.html", "topics", topics=topics)


@app.post("/api/topics/create")
async def create_topic(request: Request, name: str = Form(...), description: str = Form(""), category: str = Form("other")):
    if not authed(request):
        return unauthorized()
    await get_db_sync().upsert_topic(name=name.strip(), description=description.strip(), category=category)
    return RedirectResponse("/topics", status_code=302)


@app.post("/api/topics/close")
async def close_topic(request: Request, topic_id: int = Form(...)):
    if not authed(request):
        return unauthorized()
    await get_db_sync()._execute("UPDATE topics SET status = 'closed' WHERE id = $1", int(topic_id))
    return RedirectResponse("/topics", status_code=302)


@app.post("/api/topics/unlink")
async def unlink_topic_event(request: Request, topic_id: int = Form(...), event_id: int = Form(...)):
    if not authed(request):
        return unauthorized()
    await get_db_sync()._execute("DELETE FROM topic_events WHERE topic_id = $1 AND event_id = $2", int(topic_id), int(event_id))
    return RedirectResponse("/topics", status_code=302)


# ─── Sources & WhatsApp linking ──────────────────────────────────────────────


@app.get("/sources", response_class=HTMLResponse)
async def sources_page(request: Request):
    if not authed(request):
        return login_redirect(request)
    db = get_db_sync()
    health, bridge = await asyncio.gather(data.sources_health(db), data.bridge_status())
    runs = await db.get_last_run_any(["os_people_sync", "ideaerp_sync", "chat_digests", "topic_extraction"])
    os_snap = await data.os_snapshot()
    return render(request, "sources.html", "sources", health=health, bridge=bridge, config=data.config_status(),
                  runs=runs, os_counts=os_snap.summary_counts() if os_snap else None,
                  dash=await data.dash_overview())


@app.get("/sources/whatsapp", response_class=HTMLResponse)
async def whatsapp_page(request: Request):
    if not authed(request):
        return login_redirect(request)
    db = get_db_sync()
    return render(request, "whatsapp.html", "sources", bridge=await data.bridge_status(),
                  sync=await db.get_whatsapp_sync_status(), config=data.config_status())


@app.get("/api/whatsapp/status")
async def whatsapp_status(request: Request):
    if not authed(request):
        return unauthorized()
    data.invalidate("bridge")
    return await data.bridge_status()


@app.post("/api/whatsapp/{action}")
async def whatsapp_action(request: Request, action: str):
    if not authed(request):
        return unauthorized()
    from src.connectors.whatsapp_bridge import BridgeClient, BridgeError

    bridge = BridgeClient()
    try:
        if action == "pair":
            body = await request.json()
            result = await bridge.pair(str(body.get("phone", "")))
        elif action == "restart":
            result = await bridge.restart()
        elif action == "logout":
            result = await bridge.logout()
        elif action == "sync":
            from src import jobs
            spawn(jobs.whatsapp_sync(get_db_sync()))
            result = {"status": "started"}
        else:
            return JSONResponse({"error": "unknown action"}, status_code=404)
    except BridgeError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    data.invalidate("bridge")
    return result


@app.post("/api/jobs/{name}/run")
async def run_job(request: Request, name: str):
    if not authed(request):
        return unauthorized()
    from src import jobs

    if name not in jobs.JOBS:
        return JSONResponse({"error": "unknown job"}, status_code=404)
    spawn(jobs.JOBS[name][1](get_db_sync()))
    data.invalidate()
    return {"status": "started"}


# ─── People ──────────────────────────────────────────────────────────────────


@app.get("/people", response_class=HTMLResponse)
async def people_page(request: Request):
    if not authed(request):
        return login_redirect(request)
    db = get_db_sync()
    entity_map = await db.get_entity_map()
    for e in entity_map:
        by_source: dict[str, list[dict]] = {}
        for a in _j(e.get("aliases"), []) or []:
            by_source.setdefault(a.get("source", "?"), []).append({"id": a.get("id"), "name": a.get("alias", "")})
        e["aliases_by_source"] = by_source
    unmapped = await db.get_unmapped_whatsapp_senders()
    return render(request, "people.html", "people", entities=entity_map, unmapped=unmapped[:60])


@app.post("/api/entity/alias")
async def add_entity_alias(request: Request, entity_id: int = Form(...), source: str = Form(...), alias_name: str = Form(...)):
    if not authed(request):
        return unauthorized()
    db = get_db_sync()
    await db.upsert_alias(int(entity_id), source, alias_name.strip())
    await db.resolve_unlinked_events(source, alias_name.strip(), int(entity_id))
    return RedirectResponse("/people", status_code=302)


@app.post("/api/entity/alias/delete")
async def delete_entity_alias(request: Request, alias_id: int = Form(...)):
    if not authed(request):
        return unauthorized()
    await get_db_sync().delete_alias(int(alias_id))
    return RedirectResponse("/people", status_code=302)


# ─── System ──────────────────────────────────────────────────────────────────


@app.get("/system", response_class=HTMLResponse)
async def system_page(request: Request):
    if not authed(request):
        return login_redirect(request)
    from src import jobs

    db = get_db_sync()
    runs = await db.get_recent_runs(limit=80)
    last = await db.get_last_run_any(list(jobs.JOBS))
    usage = await db.get_ai_usage(days=30)
    agents_yaml = (CONFIG_DIR / "agents.yaml").read_text(encoding="utf-8") if (CONFIG_DIR / "agents.yaml").exists() else ""
    return render(request, "system.html", "system", runs=runs, jobs=jobs.JOBS, last=last, usage=usage,
                  mcp_clients=await db.count_oauth_clients(), config=data.config_status(), agents_yaml=agents_yaml)


@app.post("/api/mcp/revoke")
async def revoke_mcp(request: Request):
    if not authed(request):
        return unauthorized()
    await get_db_sync().revoke_all_oauth_tokens()
    return RedirectResponse("/system", status_code=302)


@app.post("/system/config")
async def save_config(request: Request, agents_yaml: str = Form(...)):
    if not authed(request):
        return login_redirect(request)
    try:
        yaml.safe_load(agents_yaml)
    except yaml.YAMLError:
        return RedirectResponse("/system?error=yaml", status_code=302)
    (CONFIG_DIR / "agents.yaml").write_text(agents_yaml, encoding="utf-8")
    return RedirectResponse("/system?saved=1", status_code=302)


@app.get("/healthz")
async def healthz():
    return {"ok": True}


# Legacy URLs from the old panel
for _old, _new in {"/pipeline": "/people", "/whatsapp": "/sources/whatsapp", "/logs": "/system",
                   "/config": "/system", "/connectors": "/sources", "/velocity": "/"}.items():
    app.add_api_route(_old, (lambda target: (lambda: RedirectResponse(target, status_code=301)))(_new), methods=["GET"])
