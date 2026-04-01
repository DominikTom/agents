"""Shoper connector - fetches order data from Shoper-based stores (mybed.pl, mittohome.pl)."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

import httpx

from src.common.config import get_env_optional
from src.common.types import ConnectorResult
from src.connectors.base import BaseConnector

logger = logging.getLogger(__name__)


class ShoperConnector(BaseConnector):
    name = "shoper"

    async def fetch(self, since: datetime | None = None) -> ConnectorResult:
        try:
            shoper_config = self.config.get("connectors", {}).get("shoper", {})
            stores = shoper_config.get("stores", [])

            if not stores:
                return self._success_result([])

            items = []

            for store in stores:
                store_name = store.get("name", "unknown")
                env_prefix = store.get("env_prefix", "")

                api_url = get_env_optional(f"{env_prefix}_API_URL")
                login = get_env_optional(f"{env_prefix}_LOGIN")
                password = get_env_optional(f"{env_prefix}_PASSWORD")

                if not all([api_url, login, password]):
                    logger.warning(f"Shoper {store_name}: missing credentials, skipping")
                    continue

                store_data = await self._fetch_store(
                    store_name, api_url, login, password, since
                )
                items.extend(store_data)

            logger.info(f"Shoper: fetched data from {len(stores)} stores")
            return self._success_result(items)

        except Exception as e:
            logger.error(f"Shoper fetch failed: {e}")
            return self._error_result(str(e))

    async def _fetch_store(
        self,
        store_name: str,
        api_url: str,
        login: str,
        password: str,
        since: datetime | None,
    ) -> list[dict]:
        """Fetch orders from a single Shoper store."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            # Authenticate
            auth_response = await client.post(
                f"{api_url}/auth",
                data={"client_id": login, "client_secret": password},
            )
            auth_response.raise_for_status()
            token = auth_response.json().get("access_token")

            headers = {"Authorization": f"Bearer {token}"}

            # Fetch today's and yesterday's orders
            today = datetime.now().strftime("%Y-%m-%d")
            yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

            items = []

            for label, date_filter in [("today", today), ("yesterday", yesterday)]:
                orders_response = await client.get(
                    f"{api_url}/orders",
                    headers=headers,
                    params={
                        "filters": f'{{"date":{{"from":"{date_filter}"}}}}',
                        "limit": 50,
                    },
                )
                orders_response.raise_for_status()
                data = orders_response.json()

                order_list = data.get("list", [])
                total_count = data.get("count", len(order_list))
                total_revenue = sum(
                    float(o.get("sum", 0)) for o in order_list
                )

                items.append({
                    "store": store_name,
                    "period": label,
                    "orders_count": total_count,
                    "revenue": round(total_revenue, 2),
                    "currency": order_list[0].get("currency_id", "PLN") if order_list else "PLN",
                })

            return items
