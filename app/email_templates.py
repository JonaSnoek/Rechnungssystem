"""Invoice e-mail rendering (HTML + plain text) with placeholder support.

Supported placeholders (both templates)::

    {vorname} {nachname} {name} {email} {datum} {uhrzeit}
    {gesamtbetrag} {betrag_nummerisch} {waehrung} {anzahl_positionen}
    {paypal_link} {paypal_benutzername} {produkte} {produkte_html}
    {rechnungsnummer} {app_name} {jahr} {monat}

``{produkte}`` renders a plain-text table, ``{produkte_html}`` an HTML table.
Rendering never raises on unknown placeholders: they are left untouched so a
typo in the admin template is visible in the preview instead of breaking the
daily billing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from html import escape

from .money import format_cents, format_decimal_input

_PLACEHOLDER_RE = re.compile(r"\{([a-z_]+)\}")

# Placeholders whose value is already safe markup and must not be escaped again.
_PRE_RENDERED = frozenset({"produkte_html"})


@dataclass
class RenderedInvoice:
    subject: str
    text: str
    html: str
    missing: list[str] = field(default_factory=list)


class SafeDict(dict):
    def __missing__(self, key: str) -> str:  # noqa: D105
        return "{" + key + "}"


def _money(cents: int, currency: str) -> str:
    return format_cents(cents, currency)


def _text_table(items, currency: str) -> str:
    rows = []
    for item in items:
        qty = item["quantity"]
        rows.append(
            f"{qty}\u00d7 {item['product_name']:<28}{format_cents(item['unit_price_cents'], currency):>12}"
        )
    if not rows:
        return "Keine Positionen."
    width = max(len(r) for r in rows)
    body = "\n".join(r.ljust(width) for r in rows)
    return body


def _html_table(items, currency: str) -> str:
    if not items:
        return "<p class=\"empty\">Keine Positionen.</p>"
    rows = []
    for item in items:
        rows.append(
            "<tr>"
            f"<td class=\"qty\">{escape(str(item['quantity']))}\u00d7</td>"
            f"<td class=\"name\">{escape(item['product_name'])}</td>"
            f"<td class=\"unit\">{escape(format_cents(item['unit_price_cents'], currency))}</td>"
            f"<td class=\"sum\">{escape(format_cents(item['total_cents'], currency))}</td>"
            "</tr>"
        )
    return (
        '<table class="items">'
        "<thead><tr><th class=\"qty\">Menge</th><th class=\"name\">Produkt</th>"
        f"<th class=\"unit\">Einzelpreis</th><th class=\"sum\">Summe</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def render(
    *,
    person,
    items: list[dict],
    total_cents: int,
    currency: str,
    paypal_link: str,
    paypal_username: str,
    invoice_number: str,
    period_date: date,
    app_name: str,
    subject_template: str,
    text_template: str,
    html_template: str,
    date_format: str = "%d.%m.%Y",
    created_at: datetime | None = None,
) -> RenderedInvoice:
    now = created_at or datetime.now()
    values = SafeDict(
        {
            "vorname": person.first_name,
            "nachname": person.last_name,
            "name": person.full_name,
            "email": person.email,
            "datum": period_date.strftime(date_format),
            "jahr": period_date.strftime("%Y"),
            "monat": period_date.strftime("%m"),
            "uhrzeit": now.strftime("%H:%M"),
            "gesamtbetrag": _money(total_cents, currency),
            "betrag_nummerisch": format_decimal_input(total_cents, currency),
            "waehrung": currency,
            "anzahl_positionen": str(len(items)),
            "paypal_link": paypal_link,
            "paypal_benutzername": paypal_username,
            "produkte": _text_table(items, currency),
            "produkte_html": _html_table(items, currency),
            "rechnungsnummer": invoice_number,
            "app_name": app_name,
        }
    )

    def render_string(template: str, *, escape_values: bool) -> str:
        if escape_values:
            # Values are escaped so a person name can never inject markup. The
            # pre-rendered item tables are the one exception: they are built from
            # already escaped cells, so escaping them again would show raw HTML.
            safe = SafeDict(
                {
                    k: escape(str(v), quote=True)
                    for k, v in values.items()
                    if k not in _PRE_RENDERED
                }
            )
            for key in _PRE_RENDERED:
                safe[key] = str(values[key])
            return _PLACEHOLDER_RE.sub(lambda m: safe.get(m.group(1)), template)
        return _PLACEHOLDER_RE.sub(lambda m: values.get(m.group(1)), template)

    # The plain-text body escapes nothing (it is plain text), the HTML body
    # escapes every injected value so person names can never inject markup.
    body_text = render_string(text_template, escape_values=False)
    body_html = render_string(html_template, escape_values=True)
    subject = render_string(subject_template, escape_values=False).replace("\n", " ").strip()

    known = set(values.keys())
    missing = sorted(
        {
            m.group(1)
            for m in _PLACEHOLDER_RE.finditer(text_template + html_template + subject_template)
            if m.group(1) not in known
        }
    )

    return RenderedInvoice(subject=subject, text=body_text, html=body_html, missing=missing)


HTML_DOCUMENT = """<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{subject}</title>
</head>
<body style="margin:0;padding:0;background:#f4f5f7;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#1b1d21;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f4f5f7;padding:24px 12px;">
<tr><td align="center">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:620px;background:#ffffff;border-radius:12px;border:1px solid #e3e5e8;overflow:hidden;">
  <tr><td style="padding:20px 28px;border-bottom:1px solid #e3e5e8;background:#1b1d21;">
    <span style="color:#ffffff;font-size:16px;font-weight:600;letter-spacing:.02em;">{app_name}</span>
  </td></tr>
  <tr><td style="padding:28px;font-size:15px;line-height:1.6;">
    {body}
    <hr style="border:none;border-top:1px solid #e3e5e8;margin:28px 0 16px;">
    <p style="margin:0;font-size:12px;color:#6b7280;">
      Automatisch erstellt. Bitte antworte auf diese E-Mail, falls etwas nicht stimmt.
    </p>
  </td></tr>
  <tr><td style="padding:16px 28px;background:#fafbfc;border-top:1px solid #e3e5e8;font-size:11px;color:#9aa1a9;">
    Rechnungsnummer {invoice_number} &middot; {period}
  </td></tr>
</table>
</td></tr>
</table>
</body>
</html>"""


def wrap_html_document(
    *, body: str, subject: str, app_name: str, invoice_number: str, period: str
) -> str:
    def sub(match: re.Match) -> str:
        return {
            "{subject}": escape(subject),
            "{app_name}": escape(app_name),
            "{body}": body,
            "{invoice_number}": escape(invoice_number),
            "{period}": escape(period),
        }[match.group(0)]

    return re.sub(
        r"\{(subject|app_name|body|invoice_number|period)\}", sub, HTML_DOCUMENT
    )
