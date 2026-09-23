"""Minimal Supabase PostgREST client (service key, server-side only)."""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)


class SupabaseREST:
    def __init__(self, url: str | None, key: str | None, timeout: float = 30.0):
        self.url = (url or "").rstrip("/")
        self.key = key or ""
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self.url and self.key)

    def _headers(self) -> dict:
        headers = {"apikey": self.key}
        # Legacy service_role keys are JWTs and go in Authorization too;
        # new-style sb_secret_ keys are accepted via `apikey` alone.
        if self.key.startswith("eyJ"):
            headers["Authorization"] = f"Bearer {self.key}"
        return headers

    async def select(self, table: str, params: dict, page_size: int = 1000, max_rows: int = 20000) -> list[dict]:
        """GET with pagination via Range headers (PostgREST caps responses at 1000 rows)."""
        if not self.configured:
            raise RuntimeError("Supabase not configured")
        rows: list[dict] = []
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            offset = 0
            while offset < max_rows:
                headers = {**self._headers(), "Range-Unit": "items", "Range": f"{offset}-{offset + page_size - 1}"}
                resp = await client.get(f"{self.url}/rest/v1/{table}", params=params, headers=headers)
                resp.raise_for_status()
                batch = resp.json()
                rows.extend(batch)
                if len(batch) < page_size:
                    break
                offset += page_size
        return rows

    async def upsert(self, table: str, rows: list[dict], on_conflict: str) -> None:
        if not self.configured:
            raise RuntimeError("Supabase not configured")
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{self.url}/rest/v1/{table}",
                params={"on_conflict": on_conflict},
                headers={
                    **self._headers(),
                    "Content-Type": "application/json",
                    "Prefer": "resolution=merge-duplicates,return=minimal",
                },
                json=rows,
            )
            if resp.status_code >= 400:
                raise RuntimeError(f"Supabase upsert {table} {resp.status_code}: {resp.text[:200]}")
