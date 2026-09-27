"""KPI block at the top of a report: chosen periods × metrics, with % vs the previous equal period.

Numbers come straight from the dash overview (build_overview → "periods"), never from the AI.
One structured payload (`kpi_data`) is stored in the report meta and rendered three ways:
Markdown (Slack, plain text, MCP), inline-styled HTML (e-mail) and a Jinja block (panel).
"""

from __future__ import annotations

import html
from datetime import date

from src.reports.profiles import KPI_METRICS, KPI_PERIODS

MONEY = {"revenue", "aov", "spend_total", "spend_meta", "spend_google"}
SPEND = {"spend_total", "spend_meta", "spend_google"}
# Headline cards (first period, "Razem"): at most four, in this order of preference
HEADLINE_ORDER = ["revenue", "orders", "aov", "spend_total", "spend_google", "spend_meta"]
SHOP_LABELS = {"Razem": "Razem", "mybed.pl": "mybed.pl", "mybed.de": "mybed.de", "mittohome.pl": "MittoHome"}


# ─── Formatting ──────────────────────────────────────────────────────────────


def fmt_value(v: float | None, metric: str, unit: bool = True) -> str:
    if v is None:
        return "—"
    s = f"{v:,.0f}".replace(",", "\u00a0")
    return f"{s}\u00a0zł" if metric in MONEY and unit else s


def fmt_pct(p: float | None) -> str:
    if p is None:
        return "—"
    sign = "+" if p > 0 else ("−" if p < 0 else "±")
    return f"{sign}{abs(p):.1f}%".replace(".", ",")


def tone(p: float | None, metric: str) -> str:
    """good / bad / neutral — spend changes are neither good nor bad by themselves."""
    if p is None or abs(p) < 2 or metric in SPEND:
        return "neutral"
    return "good" if p > 0 else "bad"


def _d(iso: str) -> str:
    return date.fromisoformat(iso).strftime("%d.%m")


def _range(a: str, b: str) -> str:
    return _d(a) if a == b else f"{_d(a)}–{_d(b)}"


# ─── Data ────────────────────────────────────────────────────────────────────


def kpi_data(overview: dict, cfg: dict) -> dict | None:
    """Selected slice of overview["periods"], small enough to keep in the report meta."""
    available = overview.get("periods") or {}
    periods = [p for p in cfg.get("periods") or [] if p in available]
    metrics = [m for m in cfg.get("metrics") or [] if m in KPI_METRICS]
    if not cfg.get("enabled") or not periods or not metrics:
        return None
    per_shop = bool(cfg.get("per_shop", True))
    out_periods = []
    for p in periods:
        per = available[p]
        rows = per["rows"] if per_shop else per["rows"][:1]
        out_periods.append({
            "key": p,
            "label": KPI_PERIODS[p],
            "range": _range(per["from"], per["to"]),
            "prev_range": _range(per["prev_from"], per["prev_to"]),
            "rows": [
                {"shop": SHOP_LABELS.get(r["shop"], r["shop"]),
                 "values": {m: {"v": (r.get(m) or {}).get("v"), "pct": (r.get(m) or {}).get("pct")} for m in metrics}}
                for r in rows
            ],
        })
    headline = [m for m in HEADLINE_ORDER if m in metrics][:4]
    return {
        "as_of": overview.get("date"),
        "note": overview.get("missing_note") or "",
        "per_shop": per_shop,
        "metrics": [{"key": m, "label": KPI_METRICS[m]} for m in metrics],
        "headline": headline,
        "periods": out_periods,
    }


def headline_cards(data: dict) -> list[dict]:
    first = data["periods"][0]
    total = first["rows"][0]["values"]
    return [
        {"label": KPI_METRICS[m], "value": fmt_value(total[m]["v"], m), "pct": fmt_pct(total[m]["pct"]),
         "tone": tone(total[m]["pct"], m), "has_pct": total[m]["pct"] is not None}
        for m in data["headline"]
    ]


def tables(data: dict) -> list[dict]:
    """Rows = metrics. Columns = shops (per period) or periods (totals only) — at most 5 columns, phone-friendly."""
    metrics = [m["key"] for m in data["metrics"]]
    out = []
    if data["per_shop"]:
        for per in data["periods"]:
            out.append({
                "title": per["label"],
                "subtitle": f"{per['range']} vs {per['prev_range']}",
                "columns": [r["shop"] for r in per["rows"]],
                "rows": [
                    {"label": KPI_METRICS[m], "unit": "zł" if m in MONEY else "",
                     "cells": [_cell(r["values"][m], m) for r in per["rows"]]}
                    for m in metrics
                ],
            })
    else:
        out.append({
            "title": "Razem",
            "subtitle": "każdy okres vs poprzedni o tej samej długości",
            "columns": [f"{p['label']}\n{p['range']}" for p in data["periods"]],
            "rows": [
                {"label": KPI_METRICS[m], "unit": "zł" if m in MONEY else "",
                 "cells": [_cell(p["rows"][0]["values"][m], m) for p in data["periods"]]}
                for m in metrics
            ],
        })
    return out


def _cell(c: dict, metric: str) -> dict:
    """Table cell: the unit (zł) lives in the row label, so wide tables still fit a phone screen."""
    return {"value": fmt_value(c.get("v"), metric, unit=False), "raw": c.get("v"),
            "pct": fmt_pct(c.get("pct")) if c.get("pct") is not None else "",
            "tone": tone(c.get("pct"), metric)}


# ─── Markdown (Slack, plain text, MCP) ───────────────────────────────────────


def kpi_markdown(data: dict | None) -> str:
    if not data:
        return ""
    out = ["## Liczby", "", "W nawiasie zmiana do poprzedniego okresu o tej samej długości."]
    if data.get("note"):
        out += ["", f"⚠️ {data['note']}"]
    for t in tables(data):
        cols = [c.replace("\n", " ") for c in t["columns"]]
        out += ["", f"**{t['title']}** · {t['subtitle']}", "", "| | " + " | ".join(cols) + " |", "|---|" + "---:|" * len(cols)]
        for r in t["rows"]:
            cells = [f"{c['value']} ({c['pct']})" if c["pct"] else c["value"] for c in r["cells"]]
            label = f"{r['label']} ({r['unit']})" if r["unit"] else r["label"]
            out.append(f"| {label} | " + " | ".join(cells) + " |")
    return "\n".join(out).replace(" ", " ")


def render_kpi(overview: dict, cfg: dict) -> str:
    """Markdown block ('## Liczby' + tables). Empty string when nothing is selected."""
    return kpi_markdown(kpi_data(overview, cfg))


# ─── E-mail (inline styles, table layout — DESIGN.md §9) ─────────────────────

INK, SOFT, MUTED, FAINT, LINE, CANVAS_CARD = "#111827", "#374151", "#4B5563", "#9CA3AF", "#E5E7EB", "#F9FAFB"
TONES = {  # text / background — Tailwind emerald/red/gray 700 on 50
    "good": ("#047857", "#ECFDF5"),
    "bad": ("#B91C1C", "#FEF2F2"),
    "neutral": ("#4B5563", "#F3F4F6"),
}
NUM = "font-variant-numeric:tabular-nums;white-space:nowrap;"


def _chip(pct: str, t: str) -> str:
    fg, bg = TONES[t]
    return (f'<span style="display:inline-block;padding:1px 6px;border-radius:6px;background:{bg};color:{fg};'
            f'font-size:11px;line-height:16px;font-weight:600;{NUM}">{html.escape(pct)}</span>')


def _compact(c: dict) -> str:
    """E-mail tables only: 7-digit amounts as '8,51 mln' so the per-shop table fits a 320–360px phone."""
    raw = c.get("raw")
    if raw is not None and abs(raw) >= 1_000_000:
        return f"{raw / 1_000_000:.2f}".replace(".", ",") + "\u00a0mln"
    return c["value"]


def kpi_email_html(data: dict | None) -> str:
    if not data:
        return ""
    e = html.escape
    first = data["periods"][0]
    parts = [
        f'<div style="margin:4px 0 6px;font-size:13px;line-height:20px;font-weight:600;color:{INK};">Liczby '
        f'<span style="font-weight:400;color:{FAINT};">· {e(first["label"].lower())} {e(first["range"])} vs {e(first["prev_range"])}</span></div>'
    ]
    # 2 × 2 headline cards (works on phones and desktop alike)
    cards = headline_cards(data)
    rows_html = []
    for i in range(0, len(cards), 2):
        cells = []
        for c in cards[i:i + 2]:
            chip = _chip(c["pct"], c["tone"]) if c["has_pct"] else ""
            cells.append(
                f'<td width="50%" valign="top" style="padding:{"0 4px 8px 0" if len(cells) == 0 else "0 0 8px 4px"};">'
                f'<div style="background:{CANVAS_CARD};border:1px solid {LINE};border-radius:10px;padding:12px 14px;">'
                f'<div style="font-size:12px;line-height:16px;color:{MUTED};">{e(c["label"])}</div>'
                f'<div style="margin-top:4px;font-size:20px;line-height:26px;font-weight:700;color:{INK};{NUM}">{e(c["value"])}</div>'
                f'<div style="margin-top:4px;">{chip}</div></div></td>'
            )
        if len(cells) == 1:
            cells.append('<td width="50%" style="padding:0 0 8px 4px;"></td>')
        rows_html.append("<tr>" + "".join(cells) + "</tr>")
    parts.append(
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="margin:0 0 4px;border-collapse:separate;">{"".join(rows_html)}</table>'
    )
    for t in tables(data):
        head = "".join(
            f'<th align="right" style="padding:6px 0 6px 4px;border-bottom:1px solid {LINE};font-size:11px;line-height:14px;'
            f'font-weight:600;color:{MUTED};">{"<br>".join(e(x) for x in col.split(chr(10)))}</th>'
            for col in t["columns"]
        )
        body = []
        for r in t["rows"]:
            tds = "".join(
                f'<td align="right" valign="top" style="padding:7px 0 7px 4px;border-bottom:1px solid {LINE};{NUM}">'
                f'<div style="font-size:12px;line-height:17px;color:{INK};">{e(_compact(c))}</div>'
                + (f'<div style="font-size:11px;line-height:15px;font-weight:600;color:{TONES[c["tone"]][0]};">{e(c["pct"])}</div>' if c["pct"] else "")
                + "</td>"
                for c in r["cells"]
            )
            unit = f' <span style="font-weight:400;color:{FAINT};">{e(r["unit"])}</span>' if r["unit"] else ""
            body.append(
                f'<tr><td valign="top" style="padding:7px 6px 7px 0;border-bottom:1px solid {LINE};font-size:12px;'
                f'line-height:17px;font-weight:500;color:{SOFT};">{e(r["label"])}{unit}</td>{tds}</tr>'
            )
        parts.append(
            f'<div style="margin:14px 0 4px;font-size:13px;line-height:18px;font-weight:600;color:{INK};">{e(t["title"])} '
            f'<span style="font-weight:400;color:{FAINT};">· {e(t["subtitle"])}</span></div>'
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">'
            f'<tr><th align="left" style="padding:6px 6px 6px 0;border-bottom:1px solid {LINE};"></th>{head}</tr>'
            + "".join(body) + "</table>"
        )
    if data.get("note"):
        parts.append(f'<div style="margin:10px 0 0;padding:8px 10px;border-radius:8px;background:#FFFBEB;color:#B45309;'
                     f'font-size:12px;line-height:17px;">{e(data["note"])}</div>')
    parts.append(
        f'<div style="margin:8px 0 0;font-size:11px;line-height:16px;color:{MUTED};">Liczby liczone automatycznie z hurtowni '
        f'dash (sprzedaż, Meta) i GA4 (Google Ads), dane do {e(_d(data["as_of"])) if data.get("as_of") else "wczoraj"}. '
        'Zmiana % względem poprzedniego okresu o tej samej długości.</div>'
    )
    return "".join(parts)
