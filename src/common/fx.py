"""PLN exchange rates (NBP table A, average rates) with an in-process cache and safe fallbacks."""

from __future__ import annotations

import logging
import time

import httpx

logger = logging.getLogger(__name__)

NBP_URL = "https://api.nbp.pl/api/exchangerates/tables/A/?format=json"
FALLBACK = {"PLN": 1.0, "EUR": 4.25, "CZK": 0.17}
TTL = 6 * 3600

_cache: tuple[float, dict] | None = None


def parse_nbp(payload) -> dict[str, float]:
    rates = {"PLN": 1.0}
    for table in payload or []:
        for r in table.get("rates", []):
            if r.get("code") and r.get("mid"):
                rates[r["code"].upper()] = float(r["mid"])
    return rates


async def pln_rates() -> dict[str, float]:
    """{"EUR": 4.2…, "CZK": 0.17…, "PLN": 1.0, …} — today's NBP average rates; fallbacks when NBP is unreachable."""
    global _cache
    if _cache and time.monotonic() - _cache[0] < TTL:
        return _cache[1]
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(NBP_URL, headers={"Accept": "application/json"})
            resp.raise_for_status()
            rates = {**FALLBACK, **parse_nbp(resp.json())}
        _cache = (time.monotonic(), rates)
        return rates
    except Exception as e:
        logger.warning(f"NBP rates unavailable, using fallbacks: {e}")
        return dict(FALLBACK)


def to_pln(amount: float, currency: str | None, rates: dict[str, float]) -> float:
    cur = (currency or "PLN").upper()
    return float(amount or 0) * rates.get(cur, FALLBACK.get(cur, 1.0))
