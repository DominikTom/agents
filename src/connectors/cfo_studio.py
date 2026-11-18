"""CFO Studio (mybed-cfo-studio.vercel.app) — finances of Delta Industries (MyBed): real result per closed month vs the books
(Incro), Margin I, the current month's forecast, revenue MTD, cost of orders still in production, Fakturownia invoices, alerts.

Read-only, through CFO Studio's MCP endpoint (`/api/mcp`, JSON-RPC `tools/call`) with an app API key (`cfk_…`, created by the owner
in CFO Studio → MCP / czat; D-077 there). CFO Studio keeps the logic and the permissions (a "viewer" key gets no bank data and no
SQL); Agents only shows the numbers. The key lives in .env (CFO_STUDIO_API_KEY) — never in the database, logs or reports.
Only Delta is in CFO Studio: MittoHome and NOMO (All Good Things) are excluded there.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from src.common.config import get_env_optional

logger = logging.getLogger(__name__)

DEFAULT_URL = "https://mybed-cfo-studio.vercel.app"
MONTHS = ["styczeń", "luty", "marzec", "kwiecień", "maj", "czerwiec", "lipiec", "sierpień", "wrzesień", "październik",
          "listopad", "grudzień"]


class CFOStudioError(RuntimeError):
    pass


def month_label(iso: str | None) -> str:
    """'2026-09-01' → 'wrzesień 2026'."""
    if not iso or len(iso) < 7:
        return ""
    return f"{MONTHS[int(iso[5:7]) - 1]} {iso[:4]}"


def pct_label(v: Any) -> str:
    return "—" if v is None else f"{float(v):.1f}%".replace(".", ",")


def _num(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _change(a: float | None, b: float | None) -> float | None:
    return None if not a or not b else round(100 * (a - b) / b, 1)


def summarize(ov: dict) -> dict:
    """cfo_overview JSON → the few numbers the panel and the reports show (amounts in PLN net, costs as in CFO Studio)."""
    months = [m for m in (ov.get("zamkniete_miesiace") or []) if isinstance(m, dict)]
    last = months[-1] if months else {}
    fc = ov.get("prognoza_biezacego_miesiaca") if isinstance(ov.get("prognoza_biezacego_miesiaca"), dict) else {}
    rev = {r.get("okres"): r for r in (ov.get("przychod_mtd_vs_m1_rok_temu") or []) if isinstance(r, dict)}
    backlog = [b for b in (ov.get("w_produkcji") or []) if isinstance(b, dict)]
    alerts = [a for a in (ov.get("alerty") or []) if isinstance(a, dict)]
    mtd = _num((rev.get("mtd") or {}).get("przychod_netto"))
    return {
        "ok": True,
        "month": month_label(last.get("month")),
        "incro_raport": bool(last.get("incro_raport")),
        "wynik_realny": _num(last.get("wynik_realny")),
        "wynik_incro": _num(last.get("wynik_incro")),
        "wynik_realny_ksiegi": _num(last.get("wynik_realny_ksiegi")),
        "wynik_realny_pct": pct_label(last.get("wynik_realny_pct")),
        "wynik_realny_3m": _num(last.get("wynik_realny_3m")),
        "marza1_realna_3m": pct_label(last.get("marza1_realna_3m_pct")),
        "marza1_incro_3m": pct_label(last.get("marza1_incro_3m_pct")),
        "ytd": ov.get("ytd") if isinstance(ov.get("ytd"), dict) else None,
        "prognoza": {
            "month": month_label(fc.get("month")),
            "as_of": fc.get("as_of"),
            "dni": f"{fc.get('days_elapsed')}/{fc.get('days_in_month')}" if fc.get("days_in_month") else "",
            "wynik": _num(fc.get("wynik_prognoza")),
            "wynik_mtd": _num(fc.get("wynik_mtd")),
            "przychod": _num(fc.get("przychod_prognoza")),
            "przychod_mtd": _num(fc.get("przychod_mtd")),
        } if fc else None,
        "mtd": {
            "przychod": mtd,
            "do": (rev.get("mtd") or {}).get("do"),
            "vs_m1_pct": _change(mtd, _num((rev.get("m1") or {}).get("przychod_netto"))),
            "vs_y1_pct": _change(mtd, _num((rev.get("y1") or {}).get("przychod_netto"))),
        } if mtd is not None else None,
        "produkcja": {
            "koszt": sum(_num(b.get("koszt")) or 0 for b in backlog if b.get("grupa") == "sklep" and b.get("etap") == "w_produkcji"),
            "zamowienia": sum(int(b.get("zamowienia") or 0) for b in backlog if b.get("grupa") == "sklep" and b.get("etap") == "w_produkcji"),
            "gotowe": sum(_num(b.get("koszt")) or 0 for b in backlog if b.get("grupa") == "sklep" and b.get("etap") == "gotowe"),
        } if backlog else None,
        "alerty": [{"level": a.get("level"), "title": a.get("title"), "message": a.get("message")} for a in alerts],
    }


class CFOStudioClient:
    def __init__(self, timeout: float = 60.0):
        self.url = (get_env_optional("CFO_STUDIO_URL") or DEFAULT_URL).rstrip("/")
        self.key = get_env_optional("CFO_STUDIO_API_KEY") or ""
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self.key)

    async def list_tools(self) -> list[dict]:
        """CFO Studio's tools for this key: [{name, title, description, inputSchema}] (tools/list)."""
        data = await self._rpc("tools/list", {})
        return [{k: t.get(k) for k in ("name", "title", "description", "inputSchema")} for t in data.get("tools", [])]

    async def call(self, tool: str, arguments: dict | None = None) -> str:
        """One CFO Studio MCP tool → its text result (JSON for data tools, markdown for cfo_guide)."""
        result = await self._rpc("tools/call", {"name": tool, "arguments": arguments or {}})
        text = "".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text")
        if result.get("isError"):
            raise CFOStudioError(text or "CFO Studio: błąd narzędzia")
        return text

    async def _rpc(self, method: str, params: dict) -> dict:
        if not self.configured:
            raise CFOStudioError("Brak CFO_STUDIO_API_KEY w .env — klucz tworzysz w CFO Studio → MCP / czat → Klucze API.")
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        headers = {"Authorization": f"Bearer {self.key}", "Accept": "application/json, text/event-stream"}
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(f"{self.url}/api/mcp", json=body, headers=headers)
        except httpx.HTTPError as e:
            raise CFOStudioError(f"CFO Studio nie odpowiada: {type(e).__name__}") from e
        if resp.status_code == 401:
            raise CFOStudioError("CFO Studio odrzucił klucz API (odwołany albo błędny) — utwórz nowy w CFO Studio → MCP / czat.")
        if resp.status_code >= 400:
            raise CFOStudioError(f"CFO Studio: HTTP {resp.status_code}")
        data = resp.json()
        if data.get("error"):
            raise CFOStudioError(f"CFO Studio: {data['error'].get('message', 'błąd MCP')}")
        return data.get("result") or {}

    async def call_json(self, tool: str, arguments: dict | None = None) -> Any:
        return json.loads(await self.call(tool, arguments))

    async def overview(self) -> dict:
        return await self.call_json("cfo_overview")

    async def summary(self) -> dict:
        """summarize(cfo_overview) or {"ok": False, "error": …} — never raises (panel and reports show the reason)."""
        try:
            return {**summarize(await self.overview()), "url": self.url}
        except CFOStudioError as e:
            return {"ok": False, "error": str(e), "url": self.url}
        except Exception as e:  # unexpected payload — keep the panel up
            logger.warning(f"CFO Studio summary failed: {type(e).__name__}")
            return {"ok": False, "error": f"Nieoczekiwana odpowiedź CFO Studio ({type(e).__name__})", "url": self.url}
