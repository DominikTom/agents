"""Bearer authentication for the MCP endpoint.

Accepts either an OAuth access token issued by /token (Claude.ai connector)
or the static MCP_API_KEY (for scripts / clients configured by hand).
"""

from __future__ import annotations

import hmac
import logging
import os

from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)


class BearerAuthMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope.get("headers", []))
            auth = headers.get(b"authorization", b"").decode()
            token = auth[7:] if auth.startswith("Bearer ") else ""
            if not token or not await self._valid(token):
                base = os.environ.get("MCP_BASE_URL", "https://agents.mybed.pl").rstrip("/")
                response = JSONResponse(
                    {"error": "invalid_token"},
                    status_code=401,
                    headers={"WWW-Authenticate": f'Bearer resource_metadata="{base}/.well-known/oauth-protected-resource"'},
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)

    async def _valid(self, token: str) -> bool:
        static = os.environ.get("MCP_API_KEY", "")
        if static and hmac.compare_digest(token, static):
            return True
        if not token.startswith("mcp_at_"):
            return False
        from src.dashboard.app import get_db_sync
        from src.mcp_server.oauth import hash_token

        try:
            return bool(await get_db_sync().check_oauth_token(hash_token(token), "access"))
        except Exception as e:
            logger.error(f"MCP token check failed: {e}")
            return False
