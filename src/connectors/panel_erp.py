"""New ERP — panel zamówień (orders.mybed.space, Supabase project panel-zamowien): orders straight from the shops.

The panel pulls every shop's orders through their APIs every 10 minutes (table `sklep_zamowienia`: Shoper
mybed.pl / mybed.de / mojepostel.cz, Shopify mittohome.pl, Medusa nomosleep.pl). That is the freshest and the most
correct sales source we have:
- today's orders minutes after they are placed (dash loads yesterday at ~6:00, the ERP export once a day),
- the shop is the shop (no ERP company in between), with the shop's own currency — mojepostel.cz sells in CZK,
  which IdeaERP and the dash record as PLN (≈6× too much),
- payment status straight from the shop.
Complete for all shops from COMPLETE_SINCE (the panel's shadow mode); older history stays in the dash.

Read-only. Only order-level, non-personal columns are selected — never the customer, notes or raw payloads.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from src.common.config import get_env_optional
from src.common.fx import pln_rates, to_pln
from src.common.timeutil import day_start, to_local, today
from src.connectors.supabase_rest import SupabaseREST

logger = logging.getLogger(__name__)

DEFAULT_URL = "https://mvumyvqoygfepfyuoamd.supabase.co"
SHOP_OF = {"mybed_pl": "mybed.pl", "mybed_de": "mybed.de", "mybed_cz": "mojepostel.cz",
           "mittohome": "mittohome.pl", "nomo": "nomosleep.pl"}
# order-level fields only (sklep_zamowienia also holds customer data — never read here)
COLUMNS = "sklep,numer,data_zam,status_typ,status_nazwa,oplacone,suma,waluta"
CANCELLED_TYPE = 4  # status_typ normalised by the panel for every platform (Shoper 4, Shopify/Medusa cancelled)
COMPLETE_SINCE = date(2026, 11, 9)


def _utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def aggregate(orders: list[dict], rates: dict[str, float]) -> list[dict]:
    """Orders → one row per (Warsaw day, shop) in the dash fact_daily_revenue shape (PLN)."""
    agg: dict[tuple[str, str], dict] = defaultdict(lambda: {"orders": 0, "paid": 0, "cancelled": 0, "gross": 0.0,
                                                            "paid_pln": 0.0, "original": 0.0, "currency": "PLN"})
    for o in orders:
        shop = SHOP_OF.get(o.get("sklep"))
        ts = o.get("data_zam")
        if not shop or not ts:
            continue
        dt = to_local(datetime.fromisoformat(ts.replace("Z", "+00:00")) if isinstance(ts, str) else ts)
        a = agg[(dt.date().isoformat(), shop)]
        amount = float(o.get("suma") or 0)
        pln = to_pln(amount, o.get("waluta"), rates)
        a["orders"] += 1
        a["gross"] += pln
        a["original"] += amount
        a["currency"] = (o.get("waluta") or "PLN").upper()
        if o.get("status_typ") == CANCELLED_TYPE:
            a["cancelled"] += 1
        elif o.get("oplacone"):
            a["paid"] += 1
            a["paid_pln"] += pln
    return [{
        "date": d, "source_shop": shop, "orders_count": a["orders"], "orders_paid": a["paid"],
        "orders_cancelled": a["cancelled"], "revenue_gross_pln": round(a["gross"], 2),
        "revenue_paid_pln": round(a["paid_pln"], 2), "revenue_gross_original": round(a["original"], 2),
        "original_currency": a["currency"],
        "avg_order_value_pln": round(a["gross"] / a["orders"], 2) if a["orders"] else 0,
    } for (d, shop), a in sorted(agg.items())]


class PanelERPClient:
    def __init__(self):
        self.rest = SupabaseREST(
            get_env_optional("PANEL_SUPABASE_URL") or DEFAULT_URL,
            get_env_optional("PANEL_SUPABASE_SERVICE_KEY"),
        )

    @property
    def configured(self) -> bool:
        return self.rest.configured

    async def orders(self, since: datetime, until: datetime) -> list[dict]:
        return await self.rest.select("sklep_zamowienia", {
            "select": COLUMNS,
            "and": f"(data_zam.gte.{_utc(since)},data_zam.lt.{_utc(until)})",
            "order": "data_zam,id",
        })

    async def daily_sales(self, first: date, last: date | None = None) -> list[dict]:
        """Per day and shop, in PLN, for [first, last] (Warsaw days)."""
        last = last or first
        rows = await self.orders(day_start(first), day_start(last + timedelta(days=1)))
        return aggregate(rows, await pln_rates())

    async def today_so_far(self) -> dict:
        """{"shops": {shop: {orders, revenue, paid_orders, paid, cancelled}}, "total": {...}, "as_of", "sync"}."""
        rows = await self.daily_sales(today())
        shops = {r["source_shop"]: {"zamowienia": r["orders_count"], "wartosc_zl": r["revenue_gross_pln"],
                                    "oplacone_zamowienia": r["orders_paid"], "oplacone_zl": r["revenue_paid_pln"],
                                    "anulowane": r["orders_cancelled"],
                                    **({"waluta_sklepu": r["original_currency"],
                                        "wartosc_w_walucie": r["revenue_gross_original"]}
                                       if r["original_currency"] != "PLN" else {})}
                 for r in rows}
        total = {k: round(sum(s[k] for s in shops.values()), 2)
                 for k in ("zamowienia", "wartosc_zl", "oplacone_zamowienia", "oplacone_zl", "anulowane")}
        sync = await self.sync_status()
        return {"zrodlo": "API sklepów (panel zamówień), odświeżane co 10 min", "sklepy": shops, "razem": total,
                "ostatnia_synchronizacja": sync.get("last"), **({"bledy_synchronizacji": sync["errors"]}
                                                                if sync.get("errors") else {})}

    async def sync_status(self) -> dict:
        """When the panel last pulled the shops, and any shop with a sync error."""
        try:
            rows = await self.rest.select("sklepy", {"select": "id,tryb,ostatni_sync,ostatni_blad"})
        except Exception as e:
            return {"last": None, "errors": [str(e)[:200]]}
        last = max((r["ostatni_sync"] for r in rows if r.get("ostatni_sync")), default=None)
        errors = [f"{SHOP_OF.get(r['id'], r['id'])}: {r['ostatni_blad'][:160]}" for r in rows if r.get("ostatni_blad")]
        return {"last": last, "errors": errors, "modes": {SHOP_OF.get(r["id"], r["id"]): r.get("tryb") for r in rows}}
