"""KPI table for the top of a report: chosen periods × metrics, with % vs the previous equal period.

Numbers come straight from the dash overview (build_overview → "periods"), so they are exact.
"""

from __future__ import annotations

from datetime import date

from src.reports.profiles import KPI_METRICS, KPI_PERIODS

MONEY = {"revenue", "aov", "spend_total", "spend_meta", "spend_google"}


def _num(v: float | None, money: bool) -> str:
    if v is None:
        return "—"
    s = f"{v:,.0f}".replace(",", " ")
    return f"{s} zł" if money else s


def _pct(p: float | None) -> str:
    if p is None:
        return ""
    sign = "+" if p > 0 else ("−" if p < 0 else "±")
    return f" ({sign}{abs(p):.1f}%)".replace(".", ",")


def _d(iso: str) -> str:
    return date.fromisoformat(iso).strftime("%d.%m")


def _range(a: str, b: str) -> str:
    return _d(a) if a == b else f"{_d(a)}–{_d(b)}"


def _cell(row: dict, metric: str) -> str:
    c = row.get(metric) or {}
    return _num(c.get("v"), metric in MONEY) + _pct(c.get("pct"))


def render_kpi(overview: dict, cfg: dict) -> str:
    """Markdown block ('## Liczby' + tables). Empty string when nothing is selected."""
    periods = [p for p in cfg.get("periods") or [] if p in (overview.get("periods") or {})]
    metrics = [m for m in cfg.get("metrics") or [] if m in KPI_METRICS]
    if not cfg.get("enabled") or not periods or not metrics:
        return ""
    head = "| | " + " | ".join(KPI_METRICS[m] for m in metrics) + " |"
    sep = "|---|" + "---|" * len(metrics)
    out = ["## Liczby", "", "W nawiasie zmiana do poprzedniego okresu o tej samej długości."]
    if cfg.get("per_shop", True):
        for p in periods:
            per = overview["periods"][p]
            out += ["", f"**{KPI_PERIODS[p]}** · {_range(per['from'], per['to'])} (vs {_range(per['prev_from'], per['prev_to'])})", "", head, sep]
            for row in per["rows"]:
                name = f"**{row['shop']}**" if row["shop"] == "Razem" else row["shop"]
                out.append(f"| {name} | " + " | ".join(_cell(row, m) for m in metrics) + " |")
    else:
        out += ["", head, sep]
        for p in periods:
            per = overview["periods"][p]
            total = per["rows"][0]
            out.append(f"| {KPI_PERIODS[p]} ({_range(per['from'], per['to'])}) | " + " | ".join(_cell(total, m) for m in metrics) + " |")
    return "\n".join(out)
