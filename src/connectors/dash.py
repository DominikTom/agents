"""Dash data warehouse (Supabase) — sales per shop, ad spend, showrooms.

ETL on the dash side refreshes yesterday's data around 06:00 Warsaw time,
so the morning report always sees a complete previous day.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import date, timedelta

from src.common.config import get_env_optional
from src.common.timeutil import today, week_start
from src.connectors.supabase_rest import SupabaseREST

logger = logging.getLogger(__name__)

DEFAULT_URL = "https://sebckrbvoghfdrppdxyt.supabase.co"
# Shops that are real sales channels (warehouse also has 'unknown'/'manual')
MAIN_SHOPS = ["mybed.pl", "mybed.de", "mittohome.pl"]


class DashClient:
    def __init__(self):
        self.rest = SupabaseREST(
            get_env_optional("DASH_SUPABASE_URL") or DEFAULT_URL,
            get_env_optional("DASH_SUPABASE_SERVICE_KEY"),
        )

    @property
    def configured(self) -> bool:
        return self.rest.configured

    async def revenue(self, since: date, until: date) -> list[dict]:
        return await self.rest.select("fact_daily_revenue", {
            "select": "date,source_shop,orders_count,orders_paid,orders_cancelled,revenue_gross_pln,revenue_paid_pln,avg_order_value_pln,revenue_gross_original,original_currency",
            "and": f"(date.gte.{since.isoformat()},date.lte.{until.isoformat()})",
            "order": "date",
        })

    async def adspend(self, since: date, until: date) -> list[dict]:
        return await self.rest.select("fact_daily_adspend", {
            "select": "date,platform,spend,clicks,impressions,conversions,conversion_value",
            "and": f"(date.gte.{since.isoformat()},date.lte.{until.isoformat()})",
        })

    async def showrooms(self, since: date, until: date) -> list[dict]:
        return await self.rest.select("sensmax_showroom_sales", {
            "select": "showroom,date,orders,revenue_pln",
            "and": f"(date.gte.{since.isoformat()},date.lte.{until.isoformat()})",
        })

    async def overview(self, ref: date | None = None) -> dict:
        """Numbers the reports need, all relative to `ref` (default: yesterday)."""
        ref = ref or (today() - timedelta(days=1))
        since = ref - timedelta(days=35)
        rev, ads, rooms = await asyncio.gather(
            self.revenue(since, ref), self.adspend(since, ref), self.showrooms(since, ref),
            return_exceptions=True,
        )
        if isinstance(rev, Exception):
            raise rev
        return build_overview(
            rev,
            [] if isinstance(ads, Exception) else ads,
            [] if isinstance(rooms, Exception) else rooms,
            ref,
        )


def _f(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _pct(a: float, b: float) -> float | None:
    return round((a - b) / b * 100, 1) if b else None


def build_overview(revenue: list[dict], ads: list[dict], rooms: list[dict], ref: date) -> dict:
    by_day_shop: dict[tuple[str, str], dict] = {}
    for r in revenue:
        by_day_shop[(r["date"], r["source_shop"])] = r

    def day_total(d: date, shops=None) -> dict:
        ds = d.isoformat()
        rows = [v for (dd, s), v in by_day_shop.items() if dd == ds and (shops is None or s in shops)]
        return {
            "orders": sum(int(r.get("orders_count") or 0) for r in rows),
            "revenue": round(sum(_f(r.get("revenue_gross_pln")) for r in rows), 2),
            "paid": round(sum(_f(r.get("revenue_paid_pln")) for r in rows), 2),
        }

    def range_total(start: date, end: date) -> dict:
        tot = {"orders": 0, "revenue": 0.0, "paid": 0.0}
        d = start
        while d <= end:
            t = day_total(d)
            for k in tot:
                tot[k] += t[k]
            d += timedelta(days=1)
        tot["revenue"] = round(tot["revenue"], 2)
        tot["paid"] = round(tot["paid"], 2)
        return tot

    spend_by_day: dict[str, float] = defaultdict(float)
    spend_by_platform_day: dict[tuple[str, str], dict] = defaultdict(lambda: {"spend": 0.0, "conv_value": 0.0, "clicks": 0})
    for a in ads:
        spend_by_day[a["date"]] += _f(a.get("spend"))
        agg = spend_by_platform_day[(a["date"], a.get("platform") or "inne")]
        agg["spend"] += _f(a.get("spend"))
        agg["conv_value"] += _f(a.get("conversion_value"))
        agg["clicks"] += int(a.get("clicks") or 0)

    def spend_range(start: date, end: date) -> float:
        total, d = 0.0, start
        while d <= end:
            total += spend_by_day.get(d.isoformat(), 0.0)
            d += timedelta(days=1)
        return round(total, 2)

    prev = ref - timedelta(days=1)
    same_wd = ref - timedelta(days=7)
    y = day_total(ref)
    shops = []
    for shop in MAIN_SHOPS:
        cur = by_day_shop.get((ref.isoformat(), shop)) or {}
        before = by_day_shop.get((prev.isoformat(), shop)) or {}
        last_week = by_day_shop.get((same_wd.isoformat(), shop)) or {}
        shops.append({
            "shop": shop,
            "orders": int(cur.get("orders_count") or 0),
            "revenue_pln": round(_f(cur.get("revenue_gross_pln")), 2),
            "revenue_original": round(_f(cur.get("revenue_gross_original")), 2),
            "currency": cur.get("original_currency") or ("EUR" if shop.endswith(".de") else "PLN"),
            "aov_pln": round(_f(cur.get("avg_order_value_pln")), 2),
            "vs_prev_day_pct": _pct(_f(cur.get("revenue_gross_pln")), _f(before.get("revenue_gross_pln"))),
            "vs_same_weekday_pct": _pct(_f(cur.get("revenue_gross_pln")), _f(last_week.get("revenue_gross_pln"))),
        })

    wk_start = week_start(ref)
    wtd = range_total(wk_start, ref)
    prev_wtd = range_total(wk_start - timedelta(days=7), ref - timedelta(days=7))
    mtd = range_total(ref.replace(day=1), ref)
    last7 = range_total(ref - timedelta(days=6), ref)
    prev7 = range_total(ref - timedelta(days=13), ref - timedelta(days=7))
    spend_y = spend_by_day.get(ref.isoformat(), 0.0)
    spend7 = spend_range(ref - timedelta(days=6), ref)
    spend_prev7 = spend_range(ref - timedelta(days=13), ref - timedelta(days=7))

    platforms = []
    for (d, platform), agg in spend_by_platform_day.items():
        if d == ref.isoformat():
            platforms.append({
                "platform": platform,
                "spend": round(agg["spend"], 2),
                "roas": round(agg["conv_value"] / agg["spend"], 2) if agg["spend"] else None,
                "clicks": agg["clicks"],
            })

    series = []
    d = ref - timedelta(days=29)
    while d <= ref:
        t = day_total(d)
        series.append({"date": d.isoformat(), "revenue": t["revenue"], "orders": t["orders"],
                       "spend": round(spend_by_day.get(d.isoformat(), 0.0), 2)})
        d += timedelta(days=1)

    room_rows: dict[str, dict] = defaultdict(lambda: {"orders_7d": 0, "revenue_7d": 0.0, "revenue_day": 0.0})
    for r in rooms:
        rd = r.get("date") or ""
        if rd > ref.isoformat() or rd < (ref - timedelta(days=6)).isoformat():
            continue  # warehouse has a few rows with future dates (data entry) — skip
        agg = room_rows[r["showroom"]]
        agg["orders_7d"] += int(r.get("orders") or 0)
        agg["revenue_7d"] += _f(r.get("revenue_pln"))
        if rd == ref.isoformat():
            agg["revenue_day"] += _f(r.get("revenue_pln"))

    return {
        "date": ref.isoformat(),
        "total": {
            **y,
            "aov": round(y["revenue"] / y["orders"], 2) if y["orders"] else None,
            "vs_prev_day_pct": _pct(y["revenue"], day_total(prev)["revenue"]),
            "vs_same_weekday_pct": _pct(y["revenue"], day_total(same_wd)["revenue"]),
        },
        "shops": shops,
        "week_to_date": {**wtd, "vs_prev_week_pct": _pct(wtd["revenue"], prev_wtd["revenue"])},
        "month_to_date": mtd,
        "last_7_days": {**last7, "vs_prev_7_pct": _pct(last7["revenue"], prev7["revenue"])},
        "marketing": {
            "spend_day": round(spend_y, 2),
            "spend_7d": spend7,
            "spend_7d_vs_prev_pct": _pct(spend7, spend_prev7),
            "mer_day": round(y["revenue"] / spend_y, 2) if spend_y else None,
            "mer_7d": round(last7["revenue"] / spend7, 2) if spend7 else None,
            "platforms": platforms,
            # MER = revenue / spend of the platforms present in the warehouse (today: Meta only)
            "platforms_covered": sorted({a.get("platform") or "inne" for a in ads}),
        },
        "showrooms": [
            {"showroom": k, "orders_7d": v["orders_7d"], "revenue_7d": round(v["revenue_7d"], 2),
             "revenue_day": round(v["revenue_day"], 2)}
            for k, v in sorted(room_rows.items(), key=lambda kv: -kv[1]["revenue_7d"])
        ],
        "series": series,
    }
