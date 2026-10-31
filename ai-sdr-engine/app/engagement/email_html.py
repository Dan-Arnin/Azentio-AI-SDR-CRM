# app/engagement/email_html.py - Branded, client-safe HTML rendering of an approved plain-text email (Azentio style)
from __future__ import annotations

import html
import re

from app.engagement.drafting import OPT_OUT_LINE

INK = "#16303d"
MINT = "#55ffbe"
MUTED = "#5b6d75"
LINE = "#dde5e7"
FONT = "Arial, Helvetica, sans-serif"
SIGNOFF_RE = re.compile(r"\n\n(Kind regards|Best regards|Regards),\n", re.IGNORECASE)


# Split a finalized body into (message, sign-off line, signature lines, opt-out)
def split_body(body: str) -> tuple[str, str, list[str], str]:
    opt_out = OPT_OUT_LINE if OPT_OUT_LINE in body else ""
    main = body.replace(OPT_OUT_LINE, "").rstrip()
    m = SIGNOFF_RE.search(main)
    if not m:
        return main, "", [], opt_out
    signature = main[m.end():].strip().splitlines()
    return main[:m.start()].rstrip(), m.group(1) + ",", signature, opt_out


# Escape text and keep single line breaks inside a paragraph
def _para(text: str) -> str:
    return html.escape(text.strip()).replace("\n", "<br>")


# Render the email as a single-column, table-based HTML document that works in Gmail/Outlook
def render(subject: str, body: str, note: str = "") -> str:
    message, signoff, signature, opt_out = split_body(body)
    paragraphs = "".join(
        f'<p style="margin:0 0 16px;font:15px/1.6 {FONT};color:{INK}">{_para(p)}</p>'
        for p in message.split("\n\n") if p.strip()
    )
    sig_html = ""
    if signature:
        name, rest = signature[0], signature[1:]
        sig_html = (
            f'<p style="margin:8px 0 4px;font:15px/1.6 {FONT};color:{INK}">{html.escape(signoff)}</p>'
            f'<table role="presentation" cellpadding="0" cellspacing="0" style="margin-top:6px"><tr>'
            f'<td style="width:3px;background:{MINT}"></td><td style="padding:2px 0 2px 12px">'
            f'<div style="font:bold 14px/1.5 {FONT};color:{INK}">{html.escape(name)}</div>'
            + "".join(f'<div style="font:13px/1.5 {FONT};color:{MUTED}">{html.escape(r)}</div>' for r in rest)
            + "</td></tr></table>"
        )
    footer = "".join(
        f'<p style="margin:0 0 6px;font:12px/1.5 {FONT};color:{MUTED}">{html.escape(t)}</p>' for t in (opt_out, note) if t
    )
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(subject)}</title></head>
<body style="margin:0;padding:0;background:#f5f7f7">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f5f7f7"><tr><td align="center" style="padding:28px 12px">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;background:#ffffff;border:1px solid {LINE};border-radius:12px">
<tr><td style="padding:24px 32px 18px;border-bottom:1px solid {LINE}">
<span style="font:bold 24px/1 {FONT};color:{INK};letter-spacing:-0.5px">azentio</span><span style="display:inline-block;width:8px;height:14px;margin-left:3px;background:{MINT};border-radius:4px 4px 0 0"></span>
</td></tr>
<tr><td style="padding:28px 32px 8px">{paragraphs}{sig_html}</td></tr>
<tr><td style="padding:20px 32px 24px"><div style="height:2px;width:48px;background:{MINT};margin-bottom:14px"></div>{footer}
<p style="margin:0;font:12px/1.5 {FONT};color:{MUTED}">Azentio · Banking, Insurance, Capital Markets and ERP software · <a href="https://www.azentio.com" style="color:{INK}">azentio.com</a></p>
</td></tr></table></td></tr></table></body></html>"""
