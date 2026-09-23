"""OAuth 2.0 (PKCE) for connecting the MCP server to Claude.ai.

Flow: Claude.ai registers a client → sends the user to /authorize → the user
must be logged into the agents panel and click "Zezwól" → Claude exchanges the
code at /token for an access token + refresh token. Tokens are random, stored
hashed in Postgres and can be revoked from the panel (System → MCP).

Previously /authorize auto-approved anyone and /token returned MCP_API_KEY for
any refresh_token value — i.e. the whole knowledge base was public.
"""

from __future__ import annotations

import base64
import hashlib
import html
import os
import secrets
import time
from urllib.parse import urlencode, urlparse

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from src.dashboard.auth import COOKIE, verify_session

router = APIRouter()

ACCESS_TTL = 60 * 60 * 24          # 1 day
REFRESH_TTL = 60 * 60 * 24 * 90    # 90 days
CODE_TTL = 300

_auth_codes: dict[str, dict] = {}
_clients: dict[str, dict] = {}

# Only redirect back to Claude (and localhost for testing)
_ALLOWED_REDIRECT_HOSTS = {"claude.ai", "www.claude.ai", "claude.com", "localhost", "127.0.0.1"}


def _base_url() -> str:
    return os.environ.get("MCP_BASE_URL", "https://agents.mybed.pl").rstrip("/")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _db():
    from src.dashboard.app import get_db_sync

    return get_db_sync()


def _redirect_ok(uri: str) -> bool:
    try:
        host = urlparse(uri).hostname or ""
    except ValueError:
        return False
    extra = {h.strip() for h in os.environ.get("OAUTH_REDIRECT_HOSTS", "").split(",") if h.strip()}
    return host in _ALLOWED_REDIRECT_HOSTS | extra or host.endswith(".claude.ai")


@router.get("/.well-known/oauth-authorization-server")
async def oauth_metadata():
    base = _base_url()
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/authorize",
        "token_endpoint": f"{base}/token",
        "registration_endpoint": f"{base}/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
    }


@router.get("/.well-known/oauth-protected-resource")
async def resource_metadata():
    base = _base_url()
    return {
        "resource": f"{base}/mcp/mcp",
        "authorization_servers": [base],
        "bearer_methods_supported": ["header"],
    }


@router.post("/register")
async def register_client(request: Request):
    body = await request.json()
    redirect_uris = [u for u in body.get("redirect_uris", []) if _redirect_ok(u)]
    if not redirect_uris:
        return JSONResponse({"error": "invalid_redirect_uri"}, status_code=400)
    client_id = secrets.token_urlsafe(16)
    _clients[client_id] = {"name": str(body.get("client_name", "claude"))[:80], "redirect_uris": redirect_uris}
    return JSONResponse(
        {
            "client_id": client_id,
            "client_name": _clients[client_id]["name"],
            "redirect_uris": redirect_uris,
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
        status_code=201,
    )


@router.get("/authorize")
async def authorize(
    request: Request,
    response_type: str,
    client_id: str,
    redirect_uri: str,
    state: str | None = None,
    code_challenge: str | None = None,
    code_challenge_method: str | None = None,
    scope: str | None = None,
):
    if response_type != "code":
        return JSONResponse({"error": "unsupported_response_type"}, status_code=400)
    if not _redirect_ok(redirect_uri):
        return JSONResponse({"error": "invalid_redirect_uri"}, status_code=400)
    if not code_challenge or code_challenge_method != "S256":
        return JSONResponse({"error": "invalid_request", "error_description": "PKCE S256 required"}, status_code=400)
    if not verify_session(request.cookies.get(COOKIE)):
        return RedirectResponse(f"/login?{urlencode({'next': str(request.url)})}", status_code=302)

    params = {
        "client_id": client_id, "redirect_uri": redirect_uri, "state": state or "",
        "code_challenge": code_challenge, "code_challenge_method": code_challenge_method,
    }
    hidden = "".join(f'<input type="hidden" name="{k}" value="{html.escape(v or "")}">' for k, v in params.items())
    client_name = html.escape(_clients.get(client_id, {}).get("name", "Claude"))
    host = html.escape(urlparse(redirect_uri).hostname or "")
    return HTMLResponse(f"""<!doctype html><html lang="pl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Autoryzacja — MyBed Agents</title>
<link rel="stylesheet" href="/static/app.css"></head>
<body class="bg-canvas app-ambient min-h-screen grid place-items-center p-4">
<form method="post" action="/authorize" class="card w-full max-w-md p-6 space-y-4">
  <div class="text-xs font-medium text-primary">Połączenie MCP</div>
  <h1 class="text-xl font-bold text-ink tracking-tight">Zezwolić {client_name} na dostęp?</h1>
  <p class="text-sm text-ink-muted">Aplikacja <b>{host}</b> uzyska odczyt bazy wiedzy agentów: maile, WhatsApp, kalendarz, raporty i sprzedaż. Dostęp odwołasz w panelu: System → Połączenia MCP.</p>
  {hidden}
  <div class="flex gap-2 pt-2">
    <button name="decision" value="allow" class="btn-primary">Zezwól</button>
    <button name="decision" value="deny" class="btn-secondary">Odmów</button>
  </div>
</form></body></html>""")


@router.post("/authorize")
async def authorize_decision(
    request: Request,
    decision: str = Form(...),
    client_id: str = Form(...),
    redirect_uri: str = Form(...),
    state: str = Form(""),
    code_challenge: str = Form(...),
    code_challenge_method: str = Form("S256"),
):
    if not verify_session(request.cookies.get(COOKIE)) or not _redirect_ok(redirect_uri):
        return JSONResponse({"error": "access_denied"}, status_code=403)
    sep = "&" if "?" in redirect_uri else "?"
    if decision != "allow":
        return RedirectResponse(f"{redirect_uri}{sep}{urlencode({'error': 'access_denied', 'state': state})}", status_code=302)

    now = time.time()
    for k in [k for k, v in _auth_codes.items() if v["expires_at"] < now]:
        del _auth_codes[k]
    code = secrets.token_urlsafe(32)
    _auth_codes[code] = {
        "client_id": client_id, "redirect_uri": redirect_uri,
        "code_challenge": code_challenge, "expires_at": now + CODE_TTL,
    }
    params = {"code": code}
    if state:
        params["state"] = state
    return RedirectResponse(f"{redirect_uri}{sep}{urlencode(params)}", status_code=302)


async def _issue(client_id: str | None) -> dict:
    db = _db()
    access = "mcp_at_" + secrets.token_urlsafe(32)
    refresh = "mcp_rt_" + secrets.token_urlsafe(32)
    await db.store_oauth_token(hash_token(access), "access", client_id, ACCESS_TTL)
    await db.store_oauth_token(hash_token(refresh), "refresh", client_id, REFRESH_TTL)
    return {"access_token": access, "token_type": "bearer", "expires_in": ACCESS_TTL, "refresh_token": refresh}


@router.post("/token")
async def token(
    grant_type: str = Form(...),
    code: str | None = Form(None),
    redirect_uri: str | None = Form(None),
    client_id: str | None = Form(None),
    code_verifier: str | None = Form(None),
    refresh_token: str | None = Form(None),
):
    if grant_type == "authorization_code":
        data = _auth_codes.pop(code or "", None)
        if not data or data["expires_at"] < time.time():
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        if redirect_uri and redirect_uri != data["redirect_uri"]:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        if not code_verifier:
            return JSONResponse({"error": "invalid_request"}, status_code=400)
        digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
        expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
        if not secrets.compare_digest(expected, data["code_challenge"]):
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        return await _issue(data["client_id"])

    if grant_type == "refresh_token":
        db = _db()
        row = await db.check_oauth_token(hash_token(refresh_token or ""), "refresh")
        if not row:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        await db.revoke_oauth_token(row["token_hash"])  # rotate
        return await _issue(row.get("client_id"))

    return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)
