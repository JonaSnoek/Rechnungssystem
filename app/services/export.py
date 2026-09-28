"""CSV export (CSV; enthaelt niemals Passwoerter oder SMTP-Secrets)."""

from __future__ import annotations

import csv
import io
from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Consumption, ConsumptionStatus, Invoice, InvoiceStatus, PaymentStatus, Person
from ..settings_service import Settings
from .billing import get_tz, local_day_bounds


def _dt(value, tz) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc).astimezone(tz).strftime("%d.%m.%Y %H:%M")
    return value.strftime("%d.%m.%Y")


def consumptions_csv(
    session: Session,
    settings: Settings,
    *,
    start: date | None = None,
    end: date | None = None,
    person_id: int | None = None,
    status: str | None = None,
) -> str:
    tz = get_tz(settings.timezone)
    stmt = (
        select(Consumption, Person.first_name, Person.last_name, Person.email)
        .join(Person, Consumption.person_id == Person.id)
        .order_by(Consumption.created_at.asc(), Consumption.id.asc())
    )
    if start is not None:
        stmt = stmt.where(Consumption.created_at >= local_day_bounds(start, tz)[0])
    if end is not None:
        stmt = stmt.where(Consumption.created_at < local_day_bounds(end, tz)[1])
    if person_id is not None:
        stmt = stmt.where(Consumption.person_id == person_id)
    if status:
        stmt = stmt.where(Consumption.status == ConsumptionStatus(status))

    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";", quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n")
    writer.writerow(
        [
            "Datum",
            "Uhrzeit",
            "Person",
            "E-Mail",
            "Produkt",
            "Menge",
            "Einzelpreis",
            "Gesamtpreis",
            "Waehrung",
            "Abrechnungsstatus",
            "Rechnung",
        ]
    )
    for row in session.execute(stmt).all():
        c, first, last, email = row
        writer.writerow(
            [
                _dt(c.created_at, tz).split(" ")[0],
                _dt(c.created_at, tz).split(" ")[-1],
                f"{first} {last}".strip(),
                email,
                c.product_name,
                c.quantity,
                f"{(c.unit_price_cents or 0) / 100:.2f}".replace(".", ","),
                f"{(c.total_cents or 0) / 100:.2f}".replace(".", ","),
                c.currency,
                _status_label(c.status),
                c.invoice.invoice_number if c.invoice else "",
            ]
        )
    return buffer.getvalue()


def invoices_csv(session: Session, settings: Settings, *, start: date | None = None, end: date | None = None) -> str:
    stmt = (
        select(Invoice, Person.first_name, Person.last_name, Person.email)
        .join(Person, Invoice.person_id == Person.id)
        .order_by(Invoice.period_date.asc(), Invoice.id.asc())
    )
    if start is not None:
        stmt = stmt.where(Invoice.period_date >= start)
    if end is not None:
        stmt = stmt.where(Invoice.period_date <= end)
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";", quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n")
    writer.writerow(
        [
            "Rechnungsnummer",
            "Datum",
            "Person",
            "E-Mail",
            "Betrag",
            "Waehrung",
            "Status",
            "Zahlungsstatus",
            "PayPal-Link",
            "Versendet am",
            "Bezahlt am",
            "Fehler",
        ]
    )
    for row in session.execute(stmt).all():
        inv, first, last, email = row
        writer.writerow(
            [
                inv.invoice_number,
                inv.period_date.strftime("%d.%m.%Y"),
                f"{first} {last}".strip(),
                email,
                f"{(inv.total_cents or 0) / 100:.2f}".replace(".", ","),
                inv.currency,
                _invoice_status_label(inv.status),
                _payment_label(inv.payment_status),
                inv.paypal_link or "",
                _dt(inv.sent_at, get_tz(settings.timezone)),
                _dt(inv.paid_at, get_tz(settings.timezone)),
                (inv.last_error or "")[:200],
            ]
        )
    return buffer.getvalue()


def _status_label(status) -> str:
    return {
        ConsumptionStatus.OFFEN: "OFFEN",
        ConsumptionStatus.ABGERECHNET: "ABGERECHNET",
        ConsumptionStatus.STORNIERT: "STORNIERT",
    }.get(status, str(status))


def _invoice_status_label(status) -> str:
    return {
        InvoiceStatus.OFFEN: "OFFEN",
        InvoiceStatus.VERSENDT: "VERSENDT",
        InvoiceStatus.FEHLGESCHLAGEN: "FEHLGESCHLAGEN",
        InvoiceStatus.STORNIERT: "STORNIERT",
    }.get(status, str(status))


def _payment_label(status) -> str:
    return {
        PaymentStatus.OFFEN: "OFFEN",
        PaymentStatus.ZAHLUNG_ANGEFORDERT: "ZAHLUNG ANGEFORDERT",
        PaymentStatus.BEZAHLT: "BEZAHLT",
        PaymentStatus.STORNIERT: "STORNIERT",
    }.get(status, str(status))
