"""Bearer token authentication middleware for MCP server."""

from __future__ import annotations

import logging
import os

from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)


class BearerAuthMiddleware:
    """ASGI middleware that validates Bearer token on MCP requests."""

    def __init__(self, app):
        self.app = app
        self.api_key = os.environ.get("MCP_API_KEY", "")

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope.get("headers", []))
            auth_header = headers.get(b"authorization", b"").decode()

            if not self.api_key:
                logger.warning("MCP_API_KEY not configured, rejecting request")
                response = JSONResponse(
                    {"error": "MCP_API_KEY not configured"}, status_code=503
                )
                await response(scope, receive, send)
                return

            if not auth_header.startswith("Bearer ") or auth_header[7:] != self.api_key:
                response = JSONResponse(
                    {"error": "Invalid or missing Bearer token"}, status_code=401
                )
                await response(scope, receive, send)
                return

            # Rewrite Host header to localhost to satisfy MCP SDK's DNS rebinding protection
            # (we're behind a reverse proxy, auth is already verified above)
            new_headers = [
                (b"host", b"localhost") if name == b"host" else (name, value)
                for name, value in scope.get("headers", [])
            ]
            scope = {**scope, "headers": new_headers}

        await self.app(scope, receive, send)
