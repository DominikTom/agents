"""IdeaERP metrics ingestion - syncs daily order metrics to business_metrics table."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone, date
from zoneinfo import ZoneInfo

import httpx

from src.common.config import get_env_optional
from src.storage.database import Database

logger = logging.getLogger(__name__)


class IdeaERPMetricsIngestor:
    """Pulls order data from IdeaERP and stores daily metrics in PostgreSQL."""

    def __init__(self, db: Database, config: dict):
        self.db = db
        self.config = config

    async def sync(self) -> dict:
        """Sync today's and yesterday's order metrics to business_metrics table."""
        stats = {"metrics_stored": 0, "errors": 0}

        try:
            erp_config = self.config.get("connectors", {}).get("ideaerp", {})
            base_url = erp_config.get("base_url", "https://api.delta.ideaerp.pl")
            env_prefix = erp_config.get("env_prefix", "IDEAERP")
            store_mapping = erp_config.get("store_mapping", [])

            token = get_env_optional(f"{env_prefix}_API_TOKEN")
            if not token:
                return {"metrics_stored": 0, "errors": 1, "error": "Missing token"}

            headers = {"Authorization": f"Bearer {token}"}

            warsaw = ZoneInfo("Europe/Warsaw")
            now = datetime.now(warsaw)
            today = now.date()
            yesterday = today - timedelta(days=1)

            async with httpx.AsyncClient(timeout=30.0) as client:
                # Get shops
                resp = await client.get(f"{base_url}/v2/shops", headers=headers)
                resp.raise_for_status()
                shops = resp.json().get("shops", [])

                for target_date in [yesterday, today]:
                    start_warsaw = datetime(
                        target_date.year, target_date.month, target_date.day,
                        tzinfo=warsaw
                    )
                    end_warsaw = start_warsaw + timedelta(days=1)
                    start_utc = start_warsaw.astimezone(timezone.utc)
                    end_utc = end_warsaw.astimezone(timezone.utc)

                    for shop in shops:
                        shop_id = shop["id"]
                        shop_name = shop["name"]

                        orders = await self._fetch_all_orders(
                            client, base_url, headers,
                            shop_id, start_utc, end_utc,
                        )

                        # Group by currency for store mapping
                        by_currency: dict[str, list[dict]] = {}
                        for order in orders:
                            cur = order.get("currency") or "PLN"
                            by_currency.setdefault(cur, []).append(order)

                        for currency, currency_orders in by_currency.items():
                            display_name = _resolve_store(shop_name, currency, store_mapping)
                            metrics = _compute_metrics(currency_orders, currency)

                            for metric_name, value in metrics.items():
                                await self.db.store_metric(
                                    metric_date=target_date,
                                    store=display_name,
                                    metric_name=metric_name,
                                    metric_value=value,
                                    currency=currency,
                                )
                                stats["metrics_stored"] += 1

        except Exception as e:
            logger.error(f"IdeaERP metrics ingestion failed: {e}")
            stats["errors"] += 1

        if stats["metrics_stored"] > 0:
            logger.info(f"IdeaERP metrics: {stats['metrics_stored']} metrics stored")

        return stats

    async def _fetch_all_orders(
        self, client: httpx.AsyncClient, base_url: str, headers: dict,
        shop_id: int, start_utc: datetime, end_utc: datetime,
    ) -> list[dict]:
        all_orders: list[dict] = []
        offset = 0
        while True:
            resp = await client.get(
                f"{base_url}/v2/orders",
                headers=headers,
                params={
                    "shop_id": shop_id,
                    "create_date_from": start_utc.strftime("%Y-%m-%dT%H:%M:%S"),
                    "create_date_to": end_utc.strftime("%Y-%m-%dT%H:%M:%S"),
                    "limit": 100,
                    "offset": offset,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            orders = data.get("orders", [])
            all_orders.extend(orders)
            if len(all_orders) >= data.get("total_count", 0) or not orders:
                break
            offset += 100
        return all_orders


def _resolve_store(shop_name: str, currency: str, mapping: list[dict]) -> str:
    for entry in mapping:
        if entry.get("shop_name") == shop_name:
            entry_cur = entry.get("currency")
            if entry_cur is None or entry_cur == currency:
                return entry.get("display_name", shop_name)
    return shop_name


def _compute_metrics(orders: list[dict], currency: str) -> dict[str, float]:
    if not orders:
        return {
            "orders_count": 0,
            "revenue": 0.0,
            "avg_order_value": 0.0,
            "paid_count": 0,
            "unpaid_count": 0,
        }

    revenue = 0.0
    paid = 0
    for order in orders:
        order_total = sum(
            float(line.get("order_line_gross", 0) or 0)
            for line in (order.get("order_lines") or [])
        )
        order_total += float(order.get("delivery_price", 0) or 0)
        revenue += order_total
        if order.get("is_paid"):
            paid += 1

    count = len(orders)
    return {
        "orders_count": float(count),
        "revenue": round(revenue, 2),
        "avg_order_value": round(revenue / count, 2) if count else 0.0,
        "paid_count": float(paid),
        "unpaid_count": float(count - paid),
    }
