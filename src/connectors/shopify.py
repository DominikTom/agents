"""Shopify connector - fetches order data from Shopify stores (mybed.de)."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

import httpx

from src.common.config import get_env_optional
from src.common.types import ConnectorResult
from src.connectors.base import BaseConnector

logger = logging.getLogger(__name__)


class ShopifyConnector(BaseConnector):
    name = "shopify"

    async def fetch(self, since: datetime | None = None) -> ConnectorResult:
        try:
            shopify_config = self.config.get("connectors", {}).get("shopify", {})
            stores = shopify_config.get("stores", [])

            if not stores:
                return self._success_result([])

            items = []

            for store in stores:
                store_name = store.get("name", "unknown")
                env_prefix = store.get("env_prefix", "")

                shop_domain = get_env_optional(f"{env_prefix}_SHOP")
                access_token = get_env_optional(f"{env_prefix}_ACCESS_TOKEN")

                if not all([shop_domain, access_token]):
                    logger.warning(f"Shopify {store_name}: missing credentials, skipping")
                    continue

                store_data = await self._fetch_store(
                    store_name, shop_domain, access_token, since
                )
                items.extend(store_data)

            logger.info(f"Shopify: fetched data from {len(stores)} stores")
            return self._success_result(items)

        except Exception as e:
            logger.error(f"Shopify fetch failed: {e}")
            return self._error_result(str(e))

    async def _fetch_store(
        self,
        store_name: str,
        shop_domain: str,
        access_token: str,
        since: datetime | None,
    ) -> list[dict]:
        """Fetch orders from a single Shopify store."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            headers = {
                "X-Shopify-Access-Token": access_token,
                "Content-Type": "application/json",
            }
            base_url = f"https://{shop_domain}/admin/api/2024-01"

            items = []

            for label, created_min in [
                ("today", datetime.now().replace(hour=0, minute=0, second=0).isoformat()),
                ("yesterday", (datetime.now() - timedelta(days=1)).replace(hour=0, minute=0, second=0).isoformat()),
            ]:
                created_max = None
                if label == "yesterday":
                    created_max = datetime.now().replace(hour=0, minute=0, second=0).isoformat()

                params = {
                    "created_at_min": created_min,
                    "status": "any",
                    "limit": 50,
                }
                if created_max:
                    params["created_at_max"] = created_max

                response = await client.get(
                    f"{base_url}/orders.json",
                    headers=headers,
                    params=params,
                )
                response.raise_for_status()
                orders = response.json().get("orders", [])

                total_revenue = sum(
                    float(o.get("total_price", 0)) for o in orders
                )
                currency = orders[0].get("currency", "EUR") if orders else "EUR"

                items.append({
                    "store": store_name,
                    "period": label,
                    "orders_count": len(orders),
                    "revenue": round(total_revenue, 2),
                    "currency": currency,
                })

            return items
