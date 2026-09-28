"""IdeaERP connector - fetches order data from IDEA Center ERP (all stores via single API)."""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

from src.common.config import get_env_optional
from src.common.types import ConnectorResult
from src.connectors.base import BaseConnector

logger = logging.getLogger(__name__)


class IdeaERPConnector(BaseConnector):
    name = "ideaerp"

    async def fetch(self, since: datetime | None = None) -> ConnectorResult:
        try:
            erp_config = self.config.get("connectors", {}).get("ideaerp", {})
            base_url = erp_config.get("base_url", "https://api.delta.ideaerp.pl")
            env_prefix = erp_config.get("env_prefix", "IDEAERP")
            store_mapping = erp_config.get("store_mapping", [])

            token = get_env_optional(f"{env_prefix}_API_TOKEN")
            if not token:
                return self._error_result("Missing IDEAERP_API_TOKEN")

            headers = {"Authorization": f"Bearer {token}"}

            async with httpx.AsyncClient(timeout=30.0) as client:
                shops = await self._fetch_shops(client, base_url, headers)
                if not shops:
                    return self._error_result("No shops returned from /v2/shops")

                # Build lookup: shop_id -> shop_name
                shop_names = {s["id"]: s["name"] for s in shops}

                items = []
                for shop in shops:
                    shop_id = shop["id"]
                    shop_name = shop["name"]
                    store_data = await self._fetch_store_orders(
                        client, base_url, headers,
                        shop_id, shop_name, store_mapping,
                    )
                    items.extend(store_data)

                logger.info(f"IdeaERP: fetched data from {len(shops)} shops -> {len(items)} items")
                return self._success_result(items)

        except Exception as e:
            logger.error(f"IdeaERP fetch failed: {e}")
            return self._error_result(str(e))

    async def _fetch_shops(
        self, client: httpx.AsyncClient, base_url: str, headers: dict
    ) -> list[dict]:
        resp = await client.get(f"{base_url}/v2/shops", headers=headers)
        resp.raise_for_status()
        return resp.json().get("shops", [])

    async def _fetch_store_orders(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        headers: dict,
        shop_id: int,
        shop_name: str,
        store_mapping: list[dict],
    ) -> list[dict]:
        # IdeaERP API stores dates in UTC, but business days are in Europe/Warsaw
        warsaw = ZoneInfo("Europe/Warsaw")
        now_warsaw = datetime.now(warsaw)
        today_start_warsaw = now_warsaw.replace(hour=0, minute=0, second=0, microsecond=0)
        yesterday_start_warsaw = today_start_warsaw - timedelta(days=1)
        day_before_start_warsaw = yesterday_start_warsaw - timedelta(days=1)

        # Convert Warsaw midnight boundaries to UTC for API queries
        today_start_utc = today_start_warsaw.astimezone(timezone.utc)
        yesterday_start_utc = yesterday_start_warsaw.astimezone(timezone.utc)
        day_before_start_utc = day_before_start_warsaw.astimezone(timezone.utc)
        now_utc = now_warsaw.astimezone(timezone.utc)

        items = []

        for label, date_from, date_to in [
            ("yesterday", yesterday_start_utc, today_start_utc),
            ("day_before_yesterday", day_before_start_utc, yesterday_start_utc),
            ("today_so_far", today_start_utc, now_utc),
        ]:
            orders = await self._fetch_orders_paginated(
                client,
                base_url,
                headers,
                params={
                    "shop_id": shop_id,
                    "create_date_from": date_from.strftime("%Y-%m-%dT%H:%M:%S"),
                    "create_date_to": date_to.strftime("%Y-%m-%dT%H:%M:%S"),
                },
            )

            # Split orders by currency to distinguish stores (e.g. mybed.pl PLN vs mybed.de EUR)
            by_currency: dict[str, list[dict]] = defaultdict(list)
            for order in orders:
                currency = order.get("currency") or "PLN"
                by_currency[currency].append(order)

            if not orders:
                # No orders - resolve display name without currency
                display_name = self._resolve_store_name(shop_name, "PLN", store_mapping)
                items.append(self._build_metrics(display_name, label, [], "PLN"))
            else:
                for currency, currency_orders in by_currency.items():
                    display_name = self._resolve_store_name(shop_name, currency, store_mapping)
                    items.append(self._build_metrics(display_name, label, currency_orders, currency))

        return items

    async def _fetch_orders_paginated(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        headers: dict,
        params: dict,
    ) -> list[dict]:
        all_orders: list[dict] = []
        offset = 0

        while True:
            query = {**params, "limit": 100, "offset": offset}
            resp = await client.get(
                f"{base_url}/v2/orders", headers=headers, params=query
            )
            resp.raise_for_status()
            data = resp.json()

            orders = data.get("orders", [])
            all_orders.extend(orders)

            total_count = data.get("total_count", 0)
            if len(all_orders) >= total_count or not orders:
                break
            offset += 100

        return all_orders

    @staticmethod
    def _resolve_store_name(shop_name: str, currency: str, mapping: list[dict]) -> str:
        """Map ERP shop name + currency to a display name using config mapping."""
        for entry in mapping:
            if entry.get("shop_name") == shop_name:
                entry_currency = entry.get("currency")
                if entry_currency is None or entry_currency == currency:
                    return entry.get("display_name", shop_name)
        return shop_name

    @staticmethod
    def _build_metrics(
        store_name: str, period: str, orders: list[dict], currency: str
    ) -> dict:
        if not orders:
            return {
                "store": store_name,
                "period": period,
                "orders_count": 0,
                "revenue": 0.0,
                "currency": currency,
                "paid_count": 0,
                "unpaid_count": 0,
                "status_breakdown": {},
                "avg_order_value": 0.0,
            }

        revenue = 0.0
        paid_count = 0
        statuses: Counter[str] = Counter()

        for order in orders:
            order_total = sum(
                float(line.get("order_line_gross", 0) or 0)
                for line in (order.get("order_lines") or [])
            )
            order_total += float(order.get("delivery_price", 0) or 0)
            revenue += order_total

            if order.get("is_paid"):
                paid_count += 1

            statuses[order.get("status", "unknown")] += 1

        orders_count = len(orders)

        return {
            "store": store_name,
            "period": period,
            "orders_count": orders_count,
            "revenue": round(revenue, 2),
            "currency": currency,
            "paid_count": paid_count,
            "unpaid_count": orders_count - paid_count,
            "status_breakdown": dict(statuses),
            "avg_order_value": round(revenue / orders_count, 2) if orders_count else 0.0,
        }


# ── Yesterday's sales straight from the ERP (fallback while the dash warehouse is not loaded yet) ──────────
# The dash builds source_shop from the order number (Shopify… = mittohome.pl, Shoper… = mybed.pl / mybed.de by
# currency, Amazon… = amazon.de); the same rule is applied here so both sources agree.
REF_KEYS = ("name", "number", "order_number", "numer", "reference", "client_order_ref", "shop_order_number",
            "external_number", "external_id", "origin")
PREFIX_SHOP = (("shopify", "mittohome.pl"), ("amazon", "amazon.de"), ("nomo", "unknown"))


def order_ref(order: dict) -> str:
    for key in REF_KEYS:
        value = order.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def classify_order(order: dict, shop_name: str) -> str:
    """source_shop the way the dash names it."""
    currency = (order.get("currency") or "PLN").upper()
    ref = order_ref(order).lower()
    for prefix, shop in PREFIX_SHOP:
        if ref.startswith(prefix):
            return shop
    if ref.startswith("shoper") or shop_name.lower().startswith("delta industries"):
        return "mybed.de" if currency == "EUR" else "mybed.pl"
    return "inne"


def order_total(order: dict) -> float:
    total = sum(float(line.get("order_line_gross", 0) or 0) for line in (order.get("order_lines") or []))
    return total + float(order.get("delivery_price", 0) or 0)


async def erp_daily_sales(day, config: dict, eur_rate: float) -> dict:
    """Orders created on `day` (Warsaw) per source_shop, in the fact_daily_revenue row shape.
    Returns {"rows": [...], "shops": [ERP shop names], "ref_key": key used for order numbers or None}."""
    erp_config = config.get("connectors", {}).get("ideaerp", {})
    base_url = erp_config.get("base_url", "https://api.delta.ideaerp.pl")
    token = get_env_optional(f"{erp_config.get('env_prefix', 'IDEAERP')}_API_TOKEN")
    if not token:
        raise RuntimeError("Brak IDEAERP_API_TOKEN")
    warsaw = ZoneInfo("Europe/Warsaw")
    start = datetime(day.year, day.month, day.day, tzinfo=warsaw)
    params = {
        "create_date_from": start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
        "create_date_to": (start + timedelta(days=1)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
    }
    headers = {"Authorization": f"Bearer {token}"}
    conn = IdeaERPConnector(config)
    agg: dict[str, dict] = defaultdict(lambda: {"orders": 0, "paid": 0, "cancelled": 0, "gross": 0.0,
                                                "gross_paid": 0.0, "original": 0.0, "currency": "PLN"})
    ref_key = None
    async with httpx.AsyncClient(timeout=30.0) as client:
        shops = await conn._fetch_shops(client, base_url, headers)
        for shop in shops:
            orders = await conn._fetch_orders_paginated(client, base_url, headers,
                                                        {"shop_id": shop["id"], **params})
            for o in orders:
                ref_key = ref_key or next((k for k in REF_KEYS if isinstance(o.get(k), str) and o[k].strip()), None)
                currency = (o.get("currency") or "PLN").upper()
                value = order_total(o)
                pln = value * eur_rate if currency == "EUR" else value
                a = agg[classify_order(o, shop.get("name") or "")]
                a["orders"] += 1
                a["gross"] += pln
                a["original"] += value
                a["currency"] = currency
                if o.get("is_paid"):
                    a["paid"] += 1
                    a["gross_paid"] += pln
                if str(o.get("status") or "").lower() in ("anulowane", "cancel", "cancelled"):
                    a["cancelled"] += 1
    rows = [{
        "date": day.isoformat(), "source_shop": shop, "orders_count": a["orders"], "orders_paid": a["paid"],
        "orders_cancelled": a["cancelled"], "revenue_gross_pln": round(a["gross"], 2),
        "revenue_paid_pln": round(a["gross_paid"], 2), "revenue_gross_original": round(a["original"], 2),
        "original_currency": a["currency"],
        "avg_order_value_pln": round(a["gross"] / a["orders"], 2) if a["orders"] else 0,
    } for shop, a in agg.items()]
    return {"rows": rows, "shops": [s.get("name") for s in shops], "ref_key": ref_key}
