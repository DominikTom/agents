"""Markdown → HTML for the panel and for e-mail (inline styles, table layout)."""

from __future__ import annotations

import html
import re

import markdown as md

_ALLOWED_HREF = re.compile(r"^(https?:|mailto:)", re.I)


def to_html(text: str) -> str:
    """Render report markdown safely: raw HTML from the model is escaped, only http(s)/mailto links survive."""
    safe = html.escape(text or "", quote=False)
    out = md.markdown(safe, extensions=["extra", "sane_lists", "nl2br"], output_format="html")

    def fix_link(m: re.Match) -> str:
        href = html.unescape(m.group(1))
        if not _ALLOWED_HREF.match(href):
            return "<a>"
        return f'<a href="{html.escape(href)}" target="_blank" rel="noopener">'

    return re.sub(r'<a href="([^"]*)"[^>]*>', fix_link, out)


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
CANVAS = "#F1F5F9"

_INLINE = [
    (r"<h1>", f'<h1 style="margin:24px 0 8px;font-size:20px;line-height:28px;font-weight:700;color:{INK};">'),
    (r"<h2>", f'<h2 style="margin:28px 0 10px;padding-top:18px;border-top:1px solid {LINE};font-size:16px;line-height:24px;font-weight:700;color:{INK};">'),
    (r"<h3>", f'<h3 style="margin:18px 0 6px;font-size:14px;line-height:20px;font-weight:700;color:{INK};">'),
    (r"<p>", f'<p style="margin:0 0 10px;font-size:14px;line-height:22px;color:{SOFT};">'),
    (r"<ul>", '<ul style="margin:0 0 12px;padding-left:20px;">'),
    (r"<ol>", '<ol style="margin:0 0 12px;padding-left:20px;">'),
    (r"<li>", f'<li style="margin:0 0 6px;font-size:14px;line-height:22px;color:{SOFT};">'),
    (r"<strong>", f'<strong style="color:{INK};font-weight:600;">'),
    (r"<table>", f'<table cellpadding="0" cellspacing="0" style="border-collapse:collapse;width:100%;margin:6px 0 14px;font-size:13px;">'),
    (r"<th>", f'<th style="text-align:left;padding:6px 8px;border-bottom:1px solid {LINE};color:{MUTED};font-weight:600;">'),
    (r"<td>", f'<td style="padding:6px 8px;border-bottom:1px solid {LINE};color:{SOFT};">'),
    (r"<blockquote>", f'<blockquote style="margin:0 0 12px;padding:8px 12px;border-left:3px solid {LINE};color:{MUTED};">'),
    (r"<hr />", f'<hr style="border:none;border-top:1px solid {LINE};margin:20px 0;" />'),
]


def email_html(*, label: str, title: str, body_html: str, url: str | None, footer: str) -> str:
    content = body_html
    for pattern, repl in _INLINE:
        content = re.sub(pattern, repl, content)
    content = re.sub(r'<a href=', f'<a style="color:{PRIMARY};text-decoration:none;font-weight:500;" href=', content)
    button = (
        f'<a href="{html.escape(url)}" style="display:inline-block;background:{PRIMARY};color:#FFFFFF;'
        f'font-size:14px;font-weight:600;text-decoration:none;padding:10px 18px;border-radius:8px;">'
        f"Otwórz w panelu</a>"
        if url else ""
    )
    return f"""<!doctype html>
<html lang="pl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title></head>
<body style="margin:0;padding:0;background:{CANVAS};font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{CANVAS};">
<tr><td align="center" style="padding:24px 12px;">
  <table role="presentation" width="640" cellpadding="0" cellspacing="0" style="max-width:640px;width:100%;">
    <tr><td style="padding:0 4px 14px;">
      <table role="presentation" cellpadding="0" cellspacing="0"><tr>
        <td style="width:14px;height:14px;border-radius:7px;background:{PRIMARY};background-image:linear-gradient(135deg,#4F46E5,#06B6D4);"></td>
        <td style="padding-left:8px;font-size:13px;font-weight:700;color:{INK};">MyBed Group</td>
        <td style="padding-left:6px;font-size:12px;color:{FAINT};">Agents</td>
      </tr></table>
    </td></tr>
    <tr><td style="background:#FFFFFF;border:1px solid {LINE};border-radius:10px;padding:28px 28px 24px;">
      <div style="font-size:12px;font-weight:600;color:{PRIMARY};margin-bottom:6px;">{html.escape(label)}</div>
      <div style="font-size:20px;line-height:28px;font-weight:700;color:{INK};margin-bottom:14px;">{html.escape(title)}</div>
      {content}
      <div style="margin-top:22px;">{button}</div>
    </td></tr>
    <tr><td style="padding:14px 6px;font-size:11px;line-height:16px;color:{FAINT};">{html.escape(footer)}</td></tr>
  </table>
</td></tr></table></body></html>"""
