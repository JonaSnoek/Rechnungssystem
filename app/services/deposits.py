"""Deposit confirmation mails.

The money side of a deposit lives in :mod:`app.services.accounts`; this module
only handles the *notification*. Keeping it separate is the point:

* the deposit is committed before the first mail attempt, so a broken SMTP
  server can never throw away money that really arrived,
* a retry re-sends the mail and never books anything a second time - the
  ledger already carries the deposit, and ``ledger_entries.deposit_id`` is
  UNIQUE so a double booking is impossible at database level.

The mail state therefore has its own columns on ``deposits`` and its own
semantics: ``OFFEN``/``FEHLGESCHLAGEN`` mean "send it again", ``GESENDET``
means "the server took it", ``NICHT_GESENDET`` means the administrator did not
want a mail at all.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime

from markupsafe import escape
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..email_templates import wrap_html_document
from ..mailer import Mailer, SendResult
from ..models import Deposit, DepositEmailStatus, Person, utcnow
from ..money import format_cents
from ..settings_service import Settings

log = logging.getLogger(__name__)

# Statuses that still want a mail.
PENDING_STATUSES = (DepositEmailStatus.OFFEN, DepositEmailStatus.FEHLGESCHLAGEN)

# Maps the stored enum value to the word used in the mail.
PAYMENT_LABELS = {
    "BAR": "Bar",
    "PAYPAL": "PayPal",
    "UEBERWEISUNG": "Überweisung",
    "SONSTIGE": "Sonstiges",
}


def payment_label(deposit: Deposit) -> str:
    value = getattr(deposit.payment_type, "value", deposit.payment_type)
    return PAYMENT_LABELS.get(str(value), str(value))


def build_deposit_message(
    deposit: Deposit,
    person: Person,
    *,
    app_name: str,
    balance_cents: int,
    date_format: str = "%d.%m.%Y",
) -> tuple[str, str, str]:
    """Subject, plain text and HTML for one deposit confirmation.

    Built in code rather than from a settings template on purpose: the deposit
    mail is a factual receipt, and a fixed wording means the numbers in it
    cannot be accidentally broken by an edited template.
    """
    currency = deposit.currency or "EUR"
    amount = format_cents(int(deposit.amount_cents), currency)
    paid_on = (deposit.paid_at or utcnow()).strftime(date_format)
    new_balance = format_cents(int(deposit.balance_after_cents), currency)
    # The row keeps the frozen before/after of the booking. balance_cents is the
    # live account value and is passed for callers that want to show it; it is
    # intentionally not used above so the mail always matches the booking.
    _ = balance_cents

    subject = f"Einzahlung {amount} gebucht - {app_name}"

    text_lines = [
        f"Hallo {person.first_name},",
        "",
        f"wir haben am {paid_on} eine Einzahlung von {amount} "
        f"({payment_label(deposit)}) für dich gebucht.",
        "",
        f"Kontostand vorher:  {format_cents(int(deposit.balance_before_cents), currency)}",
        f"Einzahlung:         {amount}",
        f"Kontostand jetzt:   {new_balance}",
        "",
        "Die Einzahlung ist damit auf deinem Konto verrechnet und wird bei der",
        "nächsten Abrechnung automatisch berücksichtigt.",
        "",
        f"Viele Grüße, {app_name}",
    ]
    if deposit.note:
        text_lines.insert(4, f"Notiz: {deposit.note}")
    text = "\n".join(text_lines)

    rows_html = "".join(
        "<tr>"
        f"<td>{_escape(label)}</td>"
        f"<td>{_escape(value)}</td>"
        "</tr>"
        for label, value in (
            ("Kontostand vorher", format_cents(int(deposit.balance_before_cents), currency)),
            ("Einzahlung", amount),
            ("Kontostand jetzt", new_balance),
        )
    )
    note_html = (
        f"<p><strong>Notiz:</strong> {_escape(deposit.note)}</p>" if deposit.note else ""
    )

    html_body = wrap_html_document(
        body=(
            "<p>Hallo "
            + _escape(person.first_name)
            + ",</p>"
            + "<p>wir haben am "
            + _escape(paid_on)
            + " eine Einzahlung von <strong>"
            + _escape(amount)
            + "</strong> ("
            + _escape(payment_label(deposit))
            + ") für dich gebucht.</p>"
            + note_html
            + "<table>"
            + rows_html
            + "</table>"
            + "<p>Die Einzahlung ist auf deinem Konto verrechnet und wird bei der "
            + "nächsten Abrechnung automatisch berücksichtigt.</p>"
            + "<p>Viele Grüße, " + _escape(app_name) + "</p>"
        ),
        subject=subject,
        app_name=app_name,
        invoice_number="",
        period=paid_on,
        # Keine Rechnungsnummer - das Layout würde sonst ein leeres Feld zeigen.
        footer=f"Eingang am {escape(paid_on)}",
    )
    return subject, text, html_body


def _escape(value) -> str:
    """Escape a value for HTML and return a **plain** ``str``.

    ``str(escape(...))`` matters here. ``markupsafe.escape`` returns a ``Markup``
    instance, which is a subclass of ``str``. When such a value is combined with
    a string literal via ``+``, Python prefers the reflected ``Markup.__radd__``
    of the subclass, and that method escapes its argument - so the literal
    markup on the left (``"<p>Hallo "``) turned into ``"&lt;p&gt;Hallo "`` and the
    recipient saw raw HTML as text. Converting to ``str`` first keeps escaping
    restricted to the inserted values.
    """
    from markupsafe import escape

    return str(escape(str(value)))


def send_deposit_confirmation(
    session: Session,
    settings: Settings,
    deposit: Deposit,
    *,
    mailer: Mailer | None = None,
    retry_attempts: int = 2,
    retry_delay: float = 1.0,
    commit: bool = True,
) -> SendResult:
    """Try to send one deposit confirmation.

    Only the mail columns change. The balance is untouched in every branch, so
    calling this repeatedly can never credit the account twice.
    """
    person = session.get(Person, deposit.person_id)
    if person is None:
        result = SendResult(ok=False, error="Person existiert nicht mehr")
        deposit.email_status = DepositEmailStatus.FEHLGESCHLAGEN
        deposit.email_error = result.error[:1000]
        if commit:
            session.commit()
        return result

    deposit.email_attempts = int(deposit.email_attempts or 0) + 1
    subject, text, html = build_deposit_message(
        deposit, person, app_name=settings.app_name, balance_cents=int(deposit.balance_after_cents)
    )

    result = SendResult(ok=False, error="nicht versendet")
    for attempt in range(1, max(1, retry_attempts) + 1):
        active = mailer or _default_mailer(settings)
        result = active.send(to=person.email, subject=subject, text_body=text, html_body=html)
        if result.ok:
            break
        log.warning(
            "Einzahlungsbestaetigung fehlgeschlagen (Versuch %s/%s) fuer %s: %s",
            attempt,
            retry_attempts,
            person.email,
            result.error,
        )
        if attempt < retry_attempts and retry_delay > 0:
            time.sleep(retry_delay * attempt)

    if result.ok:
        deposit.email_status = DepositEmailStatus.GESENDET
        deposit.email_sent_at = utcnow()
        deposit.email_error = None
        log.info("Einzahlungsbestaetigung an %s versendet", person.email)
    else:
        deposit.email_status = DepositEmailStatus.FEHLGESCHLAGEN
        deposit.email_error = (result.error or "unbekannt")[:1000]

    if commit:
        session.commit()
    return result


def _default_mailer(settings: Settings) -> Mailer:
    """The real SMTP mailer, built the same way the invoice path builds it.

    ``get_store()`` supplies the password from the secrets file, so the deposit
    mail behaves exactly like the invoice mail when SMTP is configured properly.
    """
    from .billing import build_mailer, get_store

    return build_mailer(settings, get_store())


def pending_deposits(session: Session, *, limit: int = 100) -> list[Deposit]:
    """Deposits whose confirmation mail never made it out."""
    return list(
        session.execute(
            select(Deposit)
            .where(Deposit.email_status.in_(PENDING_STATUSES))
            .order_by(Deposit.paid_at, Deposit.id)
            .limit(limit)
        ).scalars()
    )


def retry_pending_deposit_emails(
    session: Session,
    settings: Settings,
    *,
    mailer: Mailer | None = None,
    limit: int = 100,
    retry_attempts: int = 1,
    retry_delay: float = 0.0,
    commit: bool = True,
) -> dict:
    """Re-send confirmation mails that are still open.

    Only mail columns are touched. Balances are never moved here, so running
    this repeatedly - or running it after the deposit was already confirmed -
    cannot change any account.
    """
    stats = {"considered": 0, "sent": 0, "failed": 0}
    for deposit in pending_deposits(session, limit=limit):
        stats["considered"] += 1
        result = send_deposit_confirmation(
            session,
            settings,
            deposit,
            mailer=mailer,
            retry_attempts=retry_attempts,
            retry_delay=retry_delay,
            commit=False,
        )
        if result.ok:
            stats["sent"] += 1
        else:
            stats["failed"] += 1
    if commit:
        session.commit()
    return stats


__all__ = [
    "PAYMENT_LABELS",
    "PENDING_STATUSES",
    "build_deposit_message",
    "payment_label",
    "pending_deposits",
    "retry_pending_deposit_emails",
    "send_deposit_confirmation",
]
