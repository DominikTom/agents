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
# Meta ad account → shop (same mapping as the dash app, src/lib/meta-ads.ts)
META_ACCOUNT_SHOP = {
    "act_1681802382204753": "mybed.pl",
    "act_637792865917248": "mybed.de",
    "act_797212915921530": "mittohome.pl",
}
# Google Ads cost comes from GA4 (fact_daily_traffic, source='__total__'), in the
# property currency: mybed.de reports in EUR, the Polish shops in PLN.
GA4_EUR_HOSTS = {"mybed.de"}
PLATFORM_LABELS = {"meta": "Meta", "google": "Google Ads"}


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
            "select": "date,platform,account_id,spend,spend_original,original_currency",
            "and": f"(date.gte.{since.isoformat()},date.lte.{until.isoformat()})",
        })

    async def google_cost(self, since: date, until: date) -> list[dict]:
        return await self.rest.select("fact_daily_traffic", {
            "select": "date,hostname,ad_cost",
            "source": "eq.__total__",
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
        since = ref - timedelta(days=65)  # 2 × 30 days for period comparisons
        rev, meta, google, rooms = await asyncio.gather(
            self.revenue(since, ref), self.adspend(since, ref), self.google_cost(since, ref),
            self.showrooms(since, ref),
            return_exceptions=True,
        )
        if isinstance(rev, Exception):
            raise rev
        for name, res in (("Meta", meta), ("Google (GA4)", google), ("showroomy", rooms)):
            if isinstance(res, Exception):
                logger.warning(f"dash: {name} unavailable: {res}")
        ads = normalize_spend(
            [] if isinstance(meta, Exception) else meta,
            [] if isinstance(google, Exception) else google,
            rev,
        )
        return build_overview(rev, ads, [] if isinstance(rooms, Exception) else rooms, ref)


def _f(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _pct(a: float, b: float) -> float | None:
    return round((a - b) / b * 100, 1) if b else None


def normalize_spend(meta: list[dict], google: list[dict], revenue: list[dict]) -> list[dict]:
    """One list of {date, shop, platform, spend} in PLN for Meta + Google Ads."""
    # EUR→PLN per day: from Meta DE rows (spend is PLN, spend_original EUR), else from DE revenue
    rate: dict[str, float] = {}
    for r in revenue:
        if r.get("original_currency") == "EUR" and _f(r.get("revenue_gross_original")):
            rate[r["date"]] = _f(r["revenue_gross_pln"]) / _f(r["revenue_gross_original"])
    for a in meta:
        if a.get("original_currency") == "EUR" and _f(a.get("spend_original")):
            rate[a["date"]] = _f(a["spend"]) / _f(a["spend_original"])
    fallback = (sum(rate.values()) / len(rate)) if rate else 4.25

    out = []
    for a in meta:
        out.append({
            "date": a["date"],
            "shop": META_ACCOUNT_SHOP.get(a.get("account_id") or "", "inne"),
            "platform": a.get("platform") or "meta",
            "spend": _f(a.get("spend")),
        })
    for g in google:
        cost = _f(g.get("ad_cost"))
        if not cost:
            continue
        host = g.get("hostname") or "inne"
        if host in GA4_EUR_HOSTS:
            cost *= rate.get(g["date"], fallback)
        out.append({"date": g["date"], "shop": host, "platform": "google", "spend": cost})
    return out


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
    # (shop, platform) -> date -> spend
    spend_cell: dict[tuple[str, str], dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for a in ads:
        spend_by_day[a["date"]] += a["spend"]
        spend_cell[(a["shop"], a["platform"])][a["date"]] += a["spend"]

    def spend_range(start: date, end: date) -> float:
        total, d = 0.0, start
        while d <= end:
            total += spend_by_day.get(d.isoformat(), 0.0)
            d += timedelta(days=1)
        return round(total, 2)

    def cell_range(key: tuple[str, str], start: date, end: date) -> float:
        days = spend_cell.get(key, {})
        total, d = 0.0, start
        while d <= end:
            total += days.get(d.isoformat(), 0.0)
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

    platforms = sorted({p for (_, p) in spend_cell})
    by_shop = []
    for shop in MAIN_SHOPS + sorted({sh for (sh, _) in spend_cell} - set(MAIN_SHOPS)):
        row = {"shop": shop}
        for p in platforms:
            label = PLATFORM_LABELS.get(p, p)
            row[label] = {
                "dzien": cell_range((shop, p), ref, ref),
                "7_dni": cell_range((shop, p), ref - timedelta(days=6), ref),
            }
        row["suma_dzien"] = round(sum(v["dzien"] for k, v in row.items() if isinstance(v, dict)), 2)
        row["suma_7_dni"] = round(sum(v["7_dni"] for k, v in row.items() if isinstance(v, dict)), 2)
        shop_prev7 = sum(cell_range((shop, p), ref - timedelta(days=13), ref - timedelta(days=7)) for p in platforms)
        row["suma_7_dni_vs_poprz_pct"] = _pct(row["suma_7_dni"], shop_prev7)
        if row["suma_7_dni"] or shop in MAIN_SHOPS:
            by_shop.append(row)
    by_platform = []
    for p in platforms:
        by_platform.append({
            "platforma": PLATFORM_LABELS.get(p, p),
            "dzien": round(sum(cell_range((sh, pp), ref, ref) for (sh, pp) in spend_cell if pp == p), 2),
            "7_dni": round(sum(cell_range((sh, pp), ref - timedelta(days=6), ref) for (sh, pp) in spend_cell if pp == p), 2),
        })

    def period_values(shop: str | None, start: date, end: date) -> dict:
        rev = orders = 0.0
        d = start
        while d <= end:
            t = day_total(d, None if shop is None else [shop])
            rev += t["revenue"]
            orders += t["orders"]
            d += timedelta(days=1)
        keys = [k for k in spend_cell if shop is None or k[0] == shop]
        meta = sum(cell_range(k, start, end) for k in keys if k[1] == "meta")
        google = sum(cell_range(k, start, end) for k in keys if k[1] == "google")
        return {
            "revenue": round(rev, 2), "orders": int(orders),
            "aov": round(rev / orders, 2) if orders else None,
            "spend_meta": round(meta, 2), "spend_google": round(google, 2), "spend_total": round(meta + google, 2),
        }

    periods = {}
    for n in (1, 3, 7, 30):
        cur_from, prev_from, prev_to = ref - timedelta(days=n - 1), ref - timedelta(days=2 * n - 1), ref - timedelta(days=n)
        rows = []
        for shop in [None] + MAIN_SHOPS:
            c, p = period_values(shop, cur_from, ref), period_values(shop, prev_from, prev_to)
            rows.append({"shop": shop or "Razem", **{
                k: {"v": c[k], "prev": p[k], "pct": _pct(c[k] or 0, p[k] or 0)} for k in c
            }})
        periods[str(n)] = {"from": cur_from.isoformat(), "to": ref.isoformat(),
                           "prev_from": prev_from.isoformat(), "prev_to": prev_to.isoformat(), "rows": rows}

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
            "per_sklep": by_shop,
            "per_platforma": by_platform,
            "zrodla": "Meta: konta reklamowe per sklep (dash). Google Ads: koszt z GA4 per domena, mybed.de przeliczone z EUR na PLN.",
        },
        "showrooms": [
            {"showroom": k, "orders_7d": v["orders_7d"], "revenue_7d": round(v["revenue_7d"], 2),
             "revenue_day": round(v["revenue_day"], 2)}
            for k, v in sorted(room_rows.items(), key=lambda kv: -kv[1]["revenue_7d"])
        ],
        "series": series,
        "periods": periods,
    }
