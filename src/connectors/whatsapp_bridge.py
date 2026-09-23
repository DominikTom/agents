"""HTTP client for the WhatsApp bridge container."""

from __future__ import annotations

import httpx

from src.common.config import get_env_optional


class BridgeError(RuntimeError):
    pass


class BridgeClient:
    def __init__(self, url: str | None = None, token: str | None = None, timeout: float = 20.0):
        self.url = (url or get_env_optional("WHATSAPP_BRIDGE_URL") or "http://whatsapp-bridge:3001").rstrip("/")
        self.token = token if token is not None else (get_env_optional("BRIDGE_TOKEN") or "")
        self.timeout = timeout

    def _headers(self) -> dict:
        return {"x-bridge-token": self.token} if self.token else {}

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.request(method, f"{self.url}{path}", headers=self._headers(), **kwargs)
        except httpx.HTTPError as e:
            raise BridgeError(f"Bridge niedostępny: {e.__class__.__name__}") from e
        if resp.status_code == 401:
            raise BridgeError("Bridge odrzucił token (sprawdź BRIDGE_TOKEN)")
        data = resp.json() if resp.content else {}
        if resp.status_code >= 400:
            raise BridgeError(data.get("error") or f"HTTP {resp.status_code}")
        return data

    async def status(self) -> dict:
        return await self._request("GET", "/status")

    async def pair(self, phone: str) -> dict:
        return await self._request("POST", "/pair", json={"phone": phone})

    async def restart(self) -> dict:
        return await self._request("POST", "/restart")

    async def logout(self) -> dict:
        return await self._request("POST", "/logout")

    async def chats(self) -> list[dict]:
        return (await self._request("GET", "/chats")).get("chats", [])

    async def messages(self, jid: str, since: int = 0) -> list[dict]:
        return (await self._request("GET", "/messages", params={"jid": jid, "since": since})).get("messages", [])
