"""Dashboard web application - FastAPI + Jinja2 + Tailwind."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import yaml
from fastapi import FastAPI, Request, Form, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from src.common.config import load_config, PROJECT_ROOT, CONFIG_DIR
from src.dashboard.auth import verify_login, verify_session, logout
from src.storage.database import Database

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="Agents Dashboard")
templates = Jinja2Templates(directory=TEMPLATES_DIR)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Global database instance - initialized on startup
_db: Database | None = None


@app.on_event("startup")
async def startup():
    global _db
    _db = Database()
    await _db.init()
    logger.info("Dashboard database pool initialized")


@app.on_event("shutdown")
async def shutdown():
    global _db
    if _db:
        await _db.close()


# --- Helpers ---


async def get_db() -> Database:
    if _db is None:
        raise RuntimeError("Database not initialized")
    return _db


def get_session_token(request: Request) -> str | None:
    return request.cookies.get("session_token")


def require_auth(request: Request) -> bool:
    return verify_session(get_session_token(request))


def render(request: Request, template: str, **kwargs):
    """Render a template with request context."""
    return templates.TemplateResponse(request, template, kwargs)


# --- Auth routes ---


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return render(request, "login.html", error=None)


@app.post("/login")
async def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    token = verify_login(username, password)
    if token:
        response = RedirectResponse(url="/", status_code=302)
        response.set_cookie("session_token", token, httponly=True, max_age=86400)
        return response
    return render(request, "login.html", error="Nieprawidłowe dane logowania")


@app.get("/logout")
async def logout_route(request: Request):
    token = get_session_token(request)
    if token:
        logout(token)
    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie("session_token")
    return response


# --- Dashboard routes ---


@app.get("/", response_class=HTMLResponse)
async def dashboard_page(request: Request):
    if not require_auth(request):
        return RedirectResponse(url="/login", status_code=302)

    db = await get_db()
    briefing_last = await db.get_last_run("morning_briefing")
    monitor_last = await db.get_last_run("task_monitor")
    recent_reports = await db.get_reports(limit=5)
    recent_runs = await db.get_recent_runs(limit=10)

    return render(request, "dashboard.html",
        briefing_last=briefing_last,
        monitor_last=monitor_last,
        recent_reports=recent_reports,
        recent_runs=recent_runs,
    )


@app.get("/reports", response_class=HTMLResponse)
async def reports_page(request: Request, agent: str | None = None, page: int = 1):
    if not require_auth(request):
        return RedirectResponse(url="/login", status_code=302)

    db = await get_db()
    per_page = 10
    offset = (page - 1) * per_page
    reports = await db.get_reports(agent_name=agent, limit=per_page, offset=offset)
    total = await db.get_reports_count(agent_name=agent)
    total_pages = max(1, (total + per_page - 1) // per_page)

    return render(request, "reports.html",
        reports=reports,
        current_page=page,
        total_pages=total_pages,
        agent_filter=agent,
    )


@app.get("/reports/{report_id}", response_class=HTMLResponse)
async def report_detail_page(request: Request, report_id: int):
    if not require_auth(request):
        return RedirectResponse(url="/login", status_code=302)

    db = await get_db()
    report = await db.get_report(report_id)
    if not report:
        return RedirectResponse(url="/reports", status_code=302)

    return render(request, "report_detail.html", report=report)


@app.get("/velocity", response_class=HTMLResponse)
async def velocity_page(request: Request):
    if not require_auth(request):
        return RedirectResponse(url="/login", status_code=302)

    db = await get_db()
    velocity_data = await db.get_all_velocity(weeks=8)

    persons: dict[str, list] = {}
    for record in velocity_data:
        person = record["person"]
        if person not in persons:
            persons[person] = []
        persons[person].append(record)

    return render(request, "velocity.html",
        persons=persons,
        velocity_json=json.dumps(
            {p: records for p, records in persons.items()},
            ensure_ascii=False,
            default=str,
        ),
    )


@app.get("/config", response_class=HTMLResponse)
async def config_page(request: Request):
    if not require_auth(request):
        return RedirectResponse(url="/login", status_code=302)

    agents_yaml = ""
    connectors_yaml = ""

    agents_path = CONFIG_DIR / "agents.yaml"
    connectors_path = CONFIG_DIR / "connectors.yaml"

    if agents_path.exists():
        agents_yaml = agents_path.read_text(encoding="utf-8")
    if connectors_path.exists():
        connectors_yaml = connectors_path.read_text(encoding="utf-8")

    return render(request, "config.html",
        agents_yaml=agents_yaml,
        connectors_yaml=connectors_yaml,
        saved=False,
    )


@app.post("/config", response_class=HTMLResponse)
async def config_save(
    request: Request,
    agents_yaml: str = Form(...),
    connectors_yaml: str = Form(...),
):
    if not require_auth(request):
        return RedirectResponse(url="/login", status_code=302)

    error = None
    # Validate YAML
    try:
        yaml.safe_load(agents_yaml)
        yaml.safe_load(connectors_yaml)
    except yaml.YAMLError as e:
        error = f"Błąd YAML: {e}"

    if not error:
        (CONFIG_DIR / "agents.yaml").write_text(agents_yaml, encoding="utf-8")
        (CONFIG_DIR / "connectors.yaml").write_text(connectors_yaml, encoding="utf-8")

    return render(request, "config.html",
        agents_yaml=agents_yaml,
        connectors_yaml=connectors_yaml,
        saved=error is None,
        error=error,
    )


@app.get("/logs", response_class=HTMLResponse)
async def logs_page(request: Request):
    if not require_auth(request):
        return RedirectResponse(url="/login", status_code=302)

    db = await get_db()
    runs = await db.get_recent_runs(limit=100)
    return render(request, "logs.html", runs=runs)


@app.get("/connectors", response_class=HTMLResponse)
async def connectors_page(request: Request):
    if not require_auth(request):
        return RedirectResponse(url="/login", status_code=302)

    config = load_config()
    connectors_config = config.get("connectors", {})

    connector_list = [
        {"name": "gmail", "label": "Gmail", "icon": "📧"},
        {"name": "slack", "label": "Slack", "icon": "💬"},
        {"name": "asana", "label": "Asana", "icon": "📋"},
        {"name": "google_calendar", "label": "Google Calendar", "icon": "📅"},
        {"name": "google_sheets", "label": "Google Sheets", "icon": "📊"},
        {"name": "shoper", "label": "Shoper", "icon": "🛒"},
        {"name": "shopify", "label": "Shopify", "icon": "🛍️"},
        {"name": "whatsapp", "label": "WhatsApp", "icon": "📱"},
    ]

    for conn in connector_list:
        conn["config"] = connectors_config.get(conn["name"], {})
        conn["configured"] = bool(conn["config"])

    return render(request, "connectors.html", connectors=connector_list)


# --- API: Run agent now ---


@app.post("/api/run/{agent_name}")
async def run_agent_now(request: Request, agent_name: str):
    if not require_auth(request):
        return {"error": "unauthorized"}

    from src.main import run_agent

    config = load_config()

    # Run in background so we don't block the request
    asyncio.create_task(run_agent(agent_name, config))

    return {"status": "started", "agent": agent_name}
