"""IdeaERP connector - fetches order data from IDEA Center ERP (all stores via single API)."""

from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timedelta

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

            token = get_env_optional(f"{env_prefix}_API_TOKEN")
            if not token:
                return self._error_result("Missing IDEAERP_API_TOKEN")

            headers = {"Authorization": f"Bearer {token}"}

            async with httpx.AsyncClient(timeout=30.0) as client:
                shops = await self._fetch_shops(client, base_url, headers)
                if not shops:
                    return self._error_result("No shops returned from /v2/shops")

                items = []
                for shop in shops:
                    shop_id = shop["id"]
                    shop_name = shop["name"]
                    store_data = await self._fetch_store_orders(
                        client, base_url, headers, shop_id, shop_name
                    )
                    items.extend(store_data)

                logger.info(f"IdeaERP: fetched data from {len(shops)} shops")
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
    ) -> list[dict]:
        now = datetime.now()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        yesterday_start = today_start - timedelta(days=1)

        items = []

        for label, date_from, date_to in [
            ("today", today_start, now),
            ("yesterday", yesterday_start, today_start),
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
            items.append(self._build_metrics(shop_name, label, orders))

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
    def _build_metrics(shop_name: str, period: str, orders: list[dict]) -> dict:
        if not orders:
            return {
                "store": shop_name,
                "period": period,
                "orders_count": 0,
                "revenue": 0.0,
                "currency": "PLN",
                "paid_count": 0,
                "unpaid_count": 0,
                "status_breakdown": {},
                "avg_order_value": 0.0,
            }

        revenue = 0.0
        paid_count = 0
        statuses: Counter[str] = Counter()
        currency = "PLN"

        for order in orders:
            order_total = sum(
                float(line.get("order_line_gross", 0) or 0)
                for line in (order.get("order_lines") or [])
            )
            revenue += order_total

            if order.get("is_paid"):
                paid_count += 1

            statuses[order.get("status", "unknown")] += 1

            if order.get("currency"):
                currency = order["currency"]

        orders_count = len(orders)

        return {
            "store": shop_name,
            "period": period,
            "orders_count": orders_count,
            "revenue": round(revenue, 2),
            "currency": currency,
            "paid_count": paid_count,
            "unpaid_count": orders_count - paid_count,
            "status_breakdown": dict(statuses),
            "avg_order_value": round(revenue / orders_count, 2) if orders_count else 0.0,
        }
