"""Invoice e-mail rendering (HTML + plain text) with placeholder support.

Supported placeholders (both templates)::

    {vorname} {nachname} {name} {email} {datum} {uhrzeit}
    {gesamtbetrag} {betrag_nummerisch} {waehrung} {anzahl_positionen}
    {paypal_link} {paypal_benutzername} {produkte} {produkte_html}
    {rechnungsnummer} {app_name} {jahr} {monat}
    {guthaben} {noch_zu_zahlen} {kontostand} (+ jeweilig _numerisch)
    {kontenuebersicht} {kontenuebersicht_html}

``{produkte}`` renders a plain-text table, ``{produkte_html}`` an HTML table.

The account overview is mandatory: if a template does not contain
``{kontenuebersicht_html}`` / ``{kontenuebersicht}``, the block is appended
automatically, so installations that stored an older default template still
show the balance. Putting the placeholder in the template controls the position.

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
_PRE_RENDERED = frozenset({"produkte_html", "kontenuebersicht_html"})


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


def _html_account_summary(
    *,
    total_cents: int,
    credit_applied_cents: int,
    amount_due_cents: int,
    balance_cents: int,
    currency: str,
    paypal_link: str,
) -> str:
    """Account block for the invoice mail.

    Rendered as real markup so it fits the surrounding design. ``paypal_link`` is
    empty when the invoice is fully covered by credit - in that case no link is
    shown and the mail says so instead.
    """
    rows = [
        ("Verzehr", format_cents(total_cents, currency)),
        ("Vorhandenes Guthaben", format_cents(credit_applied_cents, currency)),
        ("Noch zu zahlen", format_cents(amount_due_cents, currency)),
    ]
    cells = "".join(
        '<tr>'
        f'<td style="padding:4px 0;color:#6b7280;">{escape(label)}</td>'
        f'<td style="padding:4px 0;text-align:right;font-weight:600;">{escape(value)}</td>'
        "</tr>"
        for label, value in rows
    )
    if amount_due_cents > 0 and paypal_link:
        payment = (
            '<p style="margin:16px 0 0;">'
            f'<a href="{escape(paypal_link, quote=True)}" '
            'style="display:inline-block;background:#1b1d21;color:#ffffff;'
            "padding:12px 20px;border-radius:8px;text-decoration:none;font-weight:600;"
            f'">Jetzt {escape(format_cents(amount_due_cents, currency))} '
            "über PayPal bezahlen</a></p>"
            '<p style="margin:10px 0 0;font-size:12px;color:#6b7280;">'
            f'PayPal-Link:<br><a href="{escape(paypal_link, quote=True)}">'
            f"{escape(paypal_link)}</a></p>"
        )
    else:
        payment = (
            '<p style="margin:16px 0 0;color:#0f7b3f;font-weight:600;">'
            "Diese Rechnung ist vollständig durch dein Guthaben gedeckt. "
            "Es ist kein Betrag zu zahlen.</p>"
        )
    negative = int(balance_cents) < 0
    balance_color = "#b4232a" if negative else "#0f7b3f"
    return (
        '<div style="margin:22px 0 0;padding:16px 18px;background:#fafbfc;'
        'border:1px solid #e3e5e8;border-radius:10px;">'
        '<p style="margin:0 0 10px;font-weight:600;">Deine Kontenübersicht</p>'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0">'
        f"{cells}</table>"
        '<p style="margin:12px 0 0;padding-top:10px;border-top:1px solid #e3e5e8;'
        'font-size:15px;">Dein aktueller Kontostand: '
        f'<strong style="color:{balance_color};">'
        f"{escape(format_cents(balance_cents, currency))}</strong></p>"
        f"{payment}</div>"
    )


def _text_account_summary(
    *,
    total_cents: int,
    credit_applied_cents: int,
    amount_due_cents: int,
    balance_cents: int,
    currency: str,
    paypal_link: str,
) -> str:
    lines = [
        "Deine Kontenuebersicht",
        "",
        f"Verzehr: {format_cents(total_cents, currency)}",
        f"Vorhandenes Guthaben: {format_cents(credit_applied_cents, currency)}",
        f"Noch zu zahlen: {format_cents(amount_due_cents, currency)}",
        "",
        f"Dein aktueller Kontostand: {format_cents(balance_cents, currency)}",
    ]
    if amount_due_cents > 0 and paypal_link:
        lines += [
            "",
            f"Bitte bezahle den offenen Betrag ({format_cents(amount_due_cents, currency)}) "
            "ueber PayPal:",
            paypal_link,
        ]
    else:
        lines += [
            "",
            "Diese Rechnung ist vollstaendig durch dein Guthaben gedeckt. "
            "Es ist kein Betrag zu zahlen.",
        ]
    return "\n".join(lines)


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
    credit_applied_cents: int = 0,
    amount_due_cents: int | None = None,
    balance_cents: int = 0,
) -> RenderedInvoice:
    now = created_at or datetime.now()
    if amount_due_cents is None:
        amount_due_cents = int(total_cents) - int(credit_applied_cents)
    summary_args = {
        "total_cents": int(total_cents),
        "credit_applied_cents": int(credit_applied_cents),
        "amount_due_cents": int(amount_due_cents),
        "balance_cents": int(balance_cents),
        "currency": currency,
        "paypal_link": paypal_link or "",
    }
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
            # Kontenstand, damit eine individuelle Vorlage ihn frei platzieren kann.
            "guthaben": _money(credit_applied_cents, currency),
            "guthaben_nummerisch": format_decimal_input(credit_applied_cents, currency),
            "noch_zu_zahlen": _money(amount_due_cents, currency),
            "noch_zu_zahlen_nummerisch": format_decimal_input(amount_due_cents, currency),
            "kontostand": _money(balance_cents, currency),
            "kontostand_nummerisch": format_decimal_input(balance_cents, currency),
            "kontenuebersicht": _text_account_summary(**summary_args),
            "kontenuebersicht_html": _html_account_summary(**summary_args),
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

    # The account overview is mandatory, so it is appended when an existing
    # template does not place it itself. Installations that stored the previous
    # default therefore show the balance right away, and an admin who put the
    # placeholder where they want it keeps full control of the position.
    if "{kontenuebersicht}" not in text_template:
        body_text = body_text.rstrip() + "\n\n" + _text_account_summary(**summary_args)
    if "{kontenuebersicht_html}" not in html_template:
        body_html = body_html.rstrip() + _html_account_summary(**summary_args)

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
    {footer}
  </td></tr>
</table>
</td></tr>
</table>
</body>
</html>"""


def wrap_html_document(
    *,
    body: str,
    subject: str,
    app_name: str,
    invoice_number: str,
    period: str,
    footer: str | None = None,
) -> str:
    """Put ``body`` into the shared mail layout.

    ``footer`` replaces the default "Rechnungsnummer ... · Datum" line. Mails that
    are not an invoice - the deposit confirmation, for example - have no invoice
    number and pass their own line, otherwise the layout would show an empty
    "Rechnungsnummer" label.
    """
    if footer is None:
        footer = f"Rechnungsnummer {invoice_number} &middot; {period}"

    def sub(match: re.Match) -> str:
        return {
            "{subject}": escape(subject),
            "{app_name}": escape(app_name),
            "{body}": body,
            "{footer}": footer,
        }[match.group(0)]

    return re.sub(r"\{(subject|app_name|body|footer)\}", sub, HTML_DOCUMENT)
