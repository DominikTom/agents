"""Markdown → HTML for the panel and for e-mail (inline styles, table layout)."""

from __future__ import annotations

import html
import re

import markdown as md

_ALLOWED_HREF = re.compile(r"^(https?:|mailto:)", re.I)


def _bind_amounts(text: str) -> str:
    """Non-breaking spaces inside amounts: '1 301 384 zł' never wraps as '1 301' / '384 zł'."""
    text = re.sub(r"(?<=\d) (?=\d{3}(?!\d))", "\u00a0", text)
    return re.sub(r"(?<=[\d,]) (?=(?:zł|EUR|PLN|€|%|mln|tys\.))", "\u00a0", text)


_TABLE_CELL_STYLE = re.compile(r'\sstyle="text-align: (?:left|right|center);"')


def _sanitize(out: str) -> str:
    """Allow-list pass over the generated HTML: no images, no attributes except safe links and table alignment
    (fenced code accepts `{ .class #id }` even without attr_list)."""
    out = re.sub(r"<img\b[^>]*>", "", out)

    def tag(m: re.Match) -> str:
        name, attrs = m.group(1).lower(), m.group(2) or ""
        if name == "a":
            href = re.search(r'href="([^"]*)"', attrs)
            url = html.unescape(href.group(1)) if href else ""
            if not _ALLOWED_HREF.match(url):
                return "<a>"
            return f'<a href="{html.escape(url)}" target="_blank" rel="noopener">'
        if name in ("td", "th"):
            align = _TABLE_CELL_STYLE.search(attrs)
            return f"<{name}{align.group(0) if align else ''}>"
        return f"<{name}>"

    return re.sub(r"<([a-zA-Z][a-zA-Z0-9]*)(\s[^>]*)?>", tag, out)


def to_html(text: str) -> str:
    """Render report markdown safely: raw HTML from the model is escaped, only http(s)/mailto links survive."""
    safe = _bind_amounts(html.escape(text or "", quote=False))
    # No "extra": it includes attr_list / md_in_html, which let text like `{: onclick=… }` add attributes
    out = md.markdown(safe, extensions=["tables", "fenced_code", "sane_lists", "nl2br"], output_format="html")
    return _mark_numeric_columns(_sanitize(out))


def _mark_numeric_columns(content: str) -> str:
    """class="num" on cells of numeric table columns — the panel right-aligns them (e-mail restyles inline)."""
    def table(m: re.Match) -> str:
        t = m.group(0)
        cols = _numeric_columns(t)
        if not cols:
            return t

        def row(r: re.Match) -> str:
            idx = -1

            def cell(c: re.Match) -> str:
                # every cell counts (also ones with attributes); marking twice is a no-op
                nonlocal idx
                idx += 1
                attrs = c.group(2) or ""
                if idx not in cols or 'class="num"' in attrs:
                    return c.group(0)
                return f'<{c.group(1)} class="num"{attrs}>'

            return re.sub(r"<(td|th)(\s[^>]*)?>", cell, r.group(0))

        return re.sub(r"<tr>.*?</tr>", row, t, flags=re.S)

    return re.sub(r"<table>.*?</table>", table, content, flags=re.S)


def split_lead(text: str) -> tuple[str, str]:
    """Split the model output into (lead summary, body from the first heading on)."""
    text = (text or "").strip()
    first_heading = re.search(r"^#{1,3} ", text, re.M)
    head = text[: first_heading.start()] if first_heading else text.split("\n\n", 1)[0]
    lead = " ".join(l.strip() for l in head.strip().splitlines() if l.strip() and not l.startswith("#"))
    lead = re.sub(r"(\*\*|__|\*|`)", "", lead)  # plain text: shown in cards, e-mail previews
    body = text[first_heading.start():] if first_heading else text
    return lead[:600], body.strip()


# ─── E-mail ──────────────────────────────────────────────────────────────────
# Mail clients ignore CSS variables and <style> in many cases — hexes from the
# design system palette are inlined here on purpose (docs/DESIGN.md §9).

INK = "#111827"
SOFT = "#374151"
MUTED = "#4B5563"
FAINT = "#9CA3AF"
LINE = "#E5E7EB"
PRIMARY = "#4F46E5"
PRIMARY_SOFT = "#EEF2FF"
PRIMARY_INK = "#3730A3"
CANVAS = "#F1F5F9"
NUM = "font-variant-numeric:tabular-nums;"

_INLINE = [
    (r"<h1>", f'<h1 style="margin:24px 0 8px;font-size:20px;line-height:28px;font-weight:700;color:{INK};">'),
    (r"<h2>", f'<h2 style="margin:26px 0 10px;padding-top:18px;border-top:1px solid {LINE};font-size:16px;line-height:24px;font-weight:700;color:{INK};">'),
    (r"<h3>", f'<h3 style="margin:18px 0 6px;font-size:14px;line-height:20px;font-weight:700;color:{INK};">'),
    (r"<p>", f'<p style="margin:0 0 10px;font-size:14px;line-height:22px;color:{SOFT};">'),
    (r"<ul>", '<ul style="margin:0 0 12px;padding-left:20px;">'),
    (r"<ol>", '<ol style="margin:0 0 12px;padding-left:20px;">'),
    (r"<li>", f'<li style="margin:0 0 6px;font-size:14px;line-height:22px;color:{SOFT};">'),
    (r"<strong>", f'<strong style="color:{INK};font-weight:600;">'),
    (r"<table>", '<table cellpadding="0" cellspacing="0" style="border-collapse:collapse;width:100%;margin:6px 0 14px;font-size:13px;">'),
    (r"<blockquote>", f'<blockquote style="margin:0 0 12px;padding:8px 12px;border-left:3px solid {LINE};color:{MUTED};">'),
    (r"<hr />", f'<hr style="border:none;border-top:1px solid {LINE};margin:20px 0;" />'),
]

# A table cell is "numeric" when it is a number, amount or percentage (optionally bold / with a trailing note)
# e.g. "209 049 zł", "−19,4%", "93 894,55 zł (21 541,47 EUR)", "52 (+3%)" — but not "2 (rabat dla hotelu…)"
_NUMERIC_CELL = re.compile(
    r"^[−+\-±~]?\s*[\d\u00a0 .,]+\s*(?:zł|%|EUR|€|PLN|h|szt\.?|zam\.?)?"
    r"(?:\s*\([−+\-±~]?\s*[\d\u00a0 .,]+\s*(?:zł|%|EUR|€|PLN)?\))?\s*$",
    re.I,
)


def _is_numeric(inner: str) -> bool:
    text = re.sub(r"<[^>]+>", "", inner).strip()
    return bool(text) and bool(_NUMERIC_CELL.match(text)) and any(ch.isdigit() for ch in text[:12])


def _numeric_columns(table: str) -> set[int]:
    """Columns where at least half of the non-empty body cells are numbers / amounts / percentages."""
    rows = re.findall(r"<tr>(.*?)</tr>", table, flags=re.S)
    body_cells = [re.findall(r"<td(?: [^>]*)?>(.*?)</td>", r, flags=re.S) for r in rows]
    body_cells = [r for r in body_cells if r]
    numeric_cols: set[int] = set()
    if body_cells:
        for col in range(max(len(r) for r in body_cells)):
            vals = [r[col] for r in body_cells if col < len(r) and re.sub(r"<[^>]+>", "", r[col]).strip()]
            if vals and sum(_is_numeric(v) for v in vals) * 2 >= len(vals):
                numeric_cols.add(col)
    return numeric_cols


def _style_table(table: str) -> str:
    """Right-align (tabular figures) columns that hold numbers; header cells follow their column."""
    numeric_cols = _numeric_columns(table)
    first_row = re.search(r"<tr>(.*?)</tr>", table, flags=re.S)
    ncols = len(re.findall(r"<t[dh][\s>]", first_row.group(1))) if first_row else 0

    def fix_row(m: re.Match) -> str:
        idx = -1

        def cell(c: re.Match) -> str:
            nonlocal idx
            idx += 1
            tag, inner = c.group(1), c.group(2)
            num = idx in numeric_cols
            align = "right" if num else "left"
            if tag == "th":
                style = (f"text-align:{align};padding:6px 8px;border-bottom:1px solid {LINE};color:{MUTED};"
                         "font-size:12px;font-weight:600;")
            else:
                plain = re.sub(r"<[^>]+>", "", inner).strip()
                # short plain amounts never wrap; long cells, cells with a note and wide tables may (phones)
                short = len(plain) <= 16 and "(" not in plain and ncols <= 4
                style = (f"text-align:{align};padding:6px 8px;border-bottom:1px solid {LINE};color:{SOFT};"
                         + (NUM + ("white-space:nowrap;" if short else "") if num else ""))
            return f'<{tag} class="mb-td" style="{style}">{inner}</{tag}>'

        return "<tr>" + re.sub(r"<(td|th)(?: [^>]*)?>(.*?)</\1>", cell, m.group(1), flags=re.S) + "</tr>"

    return re.sub(r"<tr>(.*?)</tr>", fix_row, table, flags=re.S)


def _style_cells(content: str) -> str:
    return re.sub(r"<table>.*?</table>", lambda m: _style_table(m.group(0)), content, flags=re.S)


def email_html(*, label: str, title: str, body_html: str, url: str | None, footer: str,
               lead: str = "", kpi_html: str = "", sources: str = "") -> str:
    content = _style_cells(body_html)  # before inlining, while tags are still bare
    for pattern, repl in _INLINE:
        content = re.sub(pattern, repl, content)
    content = re.sub(r'<a href=', f'<a style="color:{PRIMARY};text-decoration:none;font-weight:500;" href=', content)
    button = (
        f'<a href="{html.escape(url)}" style="display:inline-block;background:{PRIMARY};color:#FFFFFF;'
        f'font-size:14px;font-weight:600;text-decoration:none;padding:10px 18px;border-radius:8px;">'
        f"Otwórz w panelu</a>"
        if url else ""
    )
    lead_html = (
        f'<div style="margin:0 0 18px;padding:12px 14px;background:{PRIMARY_SOFT};border-radius:10px;'
        f'font-size:15px;line-height:23px;color:{INK};">{html.escape(lead)}</div>'
        if lead else ""
    )
    kpi_block = (
        f'<div style="margin:0 0 6px;padding:0 0 4px;">{kpi_html}</div>' if kpi_html else ""
    )
    sources_html = (
        f'<div style="margin-top:18px;padding-top:12px;border-top:1px solid {LINE};font-size:12px;line-height:18px;color:{MUTED};">'
        f'<span style="font-weight:600;color:{SOFT};">Źródła:</span> {html.escape(sources)}</div>'
        if sources else ""
    )
    preheader = html.escape(lead[:180]) if lead else ""
    return f"""<!doctype html>
<html lang="pl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light"><meta name="supported-color-schemes" content="light">
<title>{html.escape(title)}</title>
<style>
  @media only screen and (max-width: 480px) {{
    .mb-outer {{ padding: 12px 4px !important; }}
    .mb-card {{ padding: 20px 12px 18px !important; border-radius: 10px !important; }}
    .mb-num {{ font-size: 11px !important; }}
  }}
  @media only screen and (max-width: 340px) {{
    .mb-card {{ padding: 18px 8px 16px !important; }}
    .mb-num, .mb-pct {{ font-size: 10px !important; }}
    .mb-th {{ font-size: 10px !important; }}
    .mb-hcard {{ padding: 10px !important; }}
    .mb-td {{ padding: 5px 3px !important; font-size: 12px !important; }}
    .mb-hval {{ font-size: 16px !important; line-height: 22px !important; }}
  }}
</style></head>
<body style="margin:0;padding:0;background:{CANVAS};font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;-webkit-text-size-adjust:100%;">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;color:transparent;">{preheader}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{CANVAS};">
<tr><td class="mb-outer" align="center" style="padding:20px 6px;">
  <table role="presentation" width="640" cellpadding="0" cellspacing="0" style="max-width:640px;width:100%;">
    <tr><td style="padding:0 4px 14px;">
      <table role="presentation" cellpadding="0" cellspacing="0"><tr>
        <td style="width:14px;height:14px;border-radius:7px;background:{PRIMARY};background-image:linear-gradient(135deg,#4F46E5,#06B6D4);"></td>
        <td style="padding-left:8px;font-size:13px;font-weight:700;color:{INK};">MyBed Group</td>
        <td style="padding-left:6px;font-size:12px;color:{FAINT};">Agents</td>
      </tr></table>
    </td></tr>
    <tr><td class="mb-card" style="background:#FFFFFF;border:1px solid {LINE};border-radius:12px;padding:24px 16px 22px;">
      <div style="font-size:12px;font-weight:600;color:{PRIMARY};margin-bottom:6px;">{html.escape(label)}</div>
      <div style="font-size:21px;line-height:28px;font-weight:700;color:{INK};margin-bottom:16px;">{html.escape(title)}</div>
      {lead_html}
      {kpi_block}
      {content}
      {sources_html}
      <div style="margin-top:22px;">{button}</div>
    </td></tr>
    <tr><td style="padding:14px 6px;font-size:11px;line-height:16px;color:{FAINT};">{html.escape(footer)}</td></tr>
  </table>
</td></tr></table></body></html>"""
