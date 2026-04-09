"""Minimal OAuth 2.0 server for MCP authentication with Claude.ai.

Implements auto-approve OAuth 2.0 with PKCE for single-user MCP setup.
Claude.ai does the OAuth dance → gets MCP_API_KEY as access_token → uses it for MCP requests.
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import time

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from urllib.parse import urlencode

router = APIRouter()

# In-memory stores (short-lived, cleared on container restart)
_auth_codes: dict[str, dict] = {}


def _base_url() -> str:
    return os.environ.get("MCP_BASE_URL", "https://agents.mybed.pl")


@router.get("/.well-known/oauth-authorization-server")
async def oauth_metadata():
    """OAuth 2.0 Authorization Server Metadata (RFC 8414)."""
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
    """OAuth 2.0 Protected Resource Metadata."""
    base = _base_url()
    return {
        "resource": f"{base}/mcp/mcp",
        "authorization_servers": [base],
        "bearer_methods_supported": ["header"],
    }


@router.post("/register")
async def register_client(request: Request):
    """OAuth 2.0 Dynamic Client Registration (RFC 7591) - accepts all clients."""
    body = await request.json()
    client_id = secrets.token_urlsafe(16)
    return JSONResponse(
        {
            "client_id": client_id,
            "client_name": body.get("client_name", "claude"),
            "redirect_uris": body.get("redirect_uris", []),
            "grant_types": body.get("grant_types", ["authorization_code"]),
            "response_types": body.get("response_types", ["code"]),
            "token_endpoint_auth_method": "none",
        },
        status_code=201,
    )


@router.get("/authorize")
async def authorize(
    response_type: str,
    client_id: str,
    redirect_uri: str,
    state: str | None = None,
    code_challenge: str | None = None,
    code_challenge_method: str | None = None,
    scope: str | None = None,
):
    """OAuth 2.0 Authorization Endpoint - auto-approves (trusted single-user setup)."""
    if response_type != "code":
        return JSONResponse({"error": "unsupported_response_type"}, status_code=400)

    # Generate one-time authorization code
    code = secrets.token_urlsafe(32)
    _auth_codes[code] = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": code_challenge,
        "code_challenge_method": code_challenge_method,
        "expires_at": time.time() + 300,
    }

    # Cleanup expired codes
    now = time.time()
    for k in [k for k, v in _auth_codes.items() if v["expires_at"] < now]:
        del _auth_codes[k]

    # Redirect back to Claude.ai with auth code
    params = {"code": code}
    if state:
        params["state"] = state
    sep = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(f"{redirect_uri}{sep}{urlencode(params)}", status_code=302)


@router.post("/token")
async def token(
    grant_type: str = Form(...),
    code: str | None = Form(None),
    redirect_uri: str | None = Form(None),
    client_id: str | None = Form(None),
    code_verifier: str | None = Form(None),
    refresh_token: str | None = Form(None),
):
    """OAuth 2.0 Token Endpoint - returns MCP_API_KEY as access_token."""
    api_key = os.environ.get("MCP_API_KEY", "")

    if grant_type == "authorization_code":
        if not code:
            return JSONResponse({"error": "invalid_request"}, status_code=400)

        auth_data = _auth_codes.pop(code, None)
        if not auth_data or auth_data["expires_at"] < time.time():
            return JSONResponse({"error": "invalid_grant"}, status_code=400)

        # Verify PKCE code_verifier against stored code_challenge
        if auth_data.get("code_challenge") and code_verifier:
            if auth_data.get("code_challenge_method") == "S256":
                digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
                expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
                if expected != auth_data["code_challenge"]:
                    return JSONResponse({"error": "invalid_grant"}, status_code=400)

        return {
            "access_token": api_key,
            "token_type": "bearer",
            "expires_in": 86400,
            "refresh_token": secrets.token_urlsafe(32),
        }

    if grant_type == "refresh_token":
        return {
            "access_token": api_key,
            "token_type": "bearer",
            "expires_in": 86400,
            "refresh_token": secrets.token_urlsafe(32),
        }

    return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)
