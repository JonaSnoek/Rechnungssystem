"""Billing engine.

Responsibilities:

* group open consumptions per person
* create exactly one invoice per person and period (duplicate protection)
* build the PayPal.Me link and render the mail
* mark consumptions as billed **only** after a successful SMTP hand-off
* keep failed invoices open so they can be retried

Money is integer cents throughout.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from .. import email_templates as et
from ..mailer import Mailer, SendResult
from ..models import (
    BillingRun,
    Consumption,
    ConsumptionStatus,
    Invoice,
    InvoiceItem,
    InvoiceStatus,
    PaymentStatus,
    Person,
    utcnow,
)
from ..money import MoneyError
from ..paypal import PayPalLinkError, build_link
from ..secrets_store import get_store
from ..settings_service import (
    EMAIL_BODY_HTML_TEMPLATE,
    EMAIL_BODY_TEXT_TEMPLATE,
    EMAIL_SUBJECT_TEMPLATE,
    INVOICE_NUMBER_PREFIX,
    Settings,
)
from .accounts import balance_cents as account_balance_cents
from .accounts import get_or_create_account, invoice_balance_snapshot
from .accounts import invoice_credit_and_due, note_invoice_issued
from .mail_factory import build_mailer, is_email_configured

log = logging.getLogger(__name__)


class BillingError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def get_tz(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        log.warning("Unbekannte Zeitzone %s, Fallback auf Europe/Berlin", name)
        return ZoneInfo("Europe/Berlin")


def local_day_bounds(period: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """Naive-UTC bounds of a local calendar day."""
    start_local = datetime.combine(period, dtime.min, tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    return (
        start_local.astimezone(timezone.utc).replace(tzinfo=None),
        end_local.astimezone(timezone.utc).replace(tzinfo=None),
    )


def local_now(tz: ZoneInfo) -> datetime:
    return datetime.now(timezone.utc).astimezone(tz)


def local_today(tz: ZoneInfo) -> date:
    return local_now(tz).date()


# ---------------------------------------------------------------------------
# results
# ---------------------------------------------------------------------------
@dataclass
class PersonBillingResult:
    person_id: int
    person_name: str
    email: str
    invoice_id: int | None = None
    invoice_number: str = ""
    total_cents: int = 0
    status: str = "uebersprungen"
    paypal_link: str = ""
    error: str = ""
    skipped_reason: str = ""


@dataclass
class BillingResult:
    period_date: date
    trigger: str
    persons_count: int = 0
    invoices_created: int = 0
    emails_sent: int = 0
    emails_failed: int = 0
    total_cents: int = 0
    run_id: int | None = None
    is_catchup: bool = False
    items: list[PersonBillingResult] = field(default_factory=list)
    fatal_error: str = ""

    @property
    def ok(self) -> bool:
        return not self.fatal_error and self.emails_failed == 0

    def errors(self) -> list[str]:
        """Distinct failure reasons of the individual persons, in order."""
        seen: list[str] = []
        for item in self.items:
            text = (item.error or "").strip()
            if text and text not in seen:
                seen.append(text)
        return seen

    def first_error(self) -> str:
        """Shortest useful reason for a flash message.

        ``fatal_error`` only covers database level problems. A rejected
        recipient or an SMTP refusal lands in ``items[].error``, so without
        this the UI showed a bare "SMTP-Fehler" and hid the real reason.
        """
        found = self.errors()
        if not found:
            return ""
        if len(found) == 1:
            return found[0]
        return found[0] + f" (+{len(found) - 1} weitere)"

    def as_dict(self) -> dict:
        return {
            "period_date": self.period_date.isoformat(),
            "trigger": self.trigger,
            "persons_count": self.persons_count,
            "invoices_created": self.invoices_created,
            "emails_sent": self.emails_sent,
            "emails_failed": self.emails_failed,
            "total_cents": self.total_cents,
            "is_catchup": self.is_catchup,
            "fatal_error": self.fatal_error,
        }


# ---------------------------------------------------------------------------
# queries
# ---------------------------------------------------------------------------
def open_balance_cents(session: Session, person_id: int) -> int:
    """Sum of everything not yet billed to this person.

    "Open" means: not assigned to a successfully delivered invoice. There is no
    time-of-day cut-off - a booking made at 17:05 simply waits for the next
    invoice, exactly like one made at 16:55.
    """
    total = session.execute(
        select(func.coalesce(func.sum(Consumption.total_cents), 0)).where(
            Consumption.person_id == person_id,
            Consumption.status == ConsumptionStatus.OFFEN,
        )
    ).scalar_one()
    return int(total or 0)


def open_balance_cents_bulk(session: Session) -> dict[int, int]:
    rows = session.execute(
        select(Consumption.person_id, func.sum(Consumption.total_cents))
        .where(Consumption.status == ConsumptionStatus.OFFEN)
        .group_by(Consumption.person_id)
    ).all()
    return {int(pid): int(total or 0) for pid, total in rows}


def open_consumptions_for_person(
    session: Session,
    person_id: int,
    until_utc: datetime | None = None,
    *,
    unassigned_only: bool = False,
) -> list[Consumption]:
    """Open bookings of a person, oldest first.

    ``unassigned_only`` restricts the result to bookings that are not attached
    to any invoice yet. Those - and only those - may be put into a *new*
    invoice; bookings that failed to send are retried on their own invoice.
    """
    stmt = (
        select(Consumption)
        .where(
            Consumption.person_id == person_id,
            Consumption.status == ConsumptionStatus.OFFEN,
        )
        .order_by(Consumption.created_at.asc(), Consumption.id.asc())
    )
    if unassigned_only:
        stmt = stmt.where(Consumption.invoice_id.is_(None))
    if until_utc is not None:
        stmt = stmt.where(Consumption.created_at < until_utc)
    return list(session.execute(stmt).scalars())


def persons_with_open_consumptions(
    session: Session, until_utc: datetime | None = None
) -> list[int]:
    stmt = select(Consumption.person_id).where(
        Consumption.status == ConsumptionStatus.OFFEN
    )
    if until_utc is not None:
        stmt = stmt.where(Consumption.created_at < until_utc)
    stmt = (
        stmt.group_by(Consumption.person_id)
        .order_by(Consumption.person_id.asc())
    )
    return [int(row) for row in session.execute(stmt).scalars()]


def group_consumptions(consumptions: list[Consumption]) -> list[dict]:
    """Aggregate by product name + unit price so the mail stays compact."""
    groups: dict[tuple[str, int], dict] = {}
    for c in consumptions:
        key = (c.product_name, int(c.unit_price_cents))
        entry = groups.get(key)
        if entry is None:
            entry = {
                "product_name": c.product_name,
                "unit_price_cents": int(c.unit_price_cents),
                "quantity": 0,
                "total_cents": 0,
                "consumptions": [],
            }
            groups[key] = entry
        entry["quantity"] += int(c.quantity)
        entry["total_cents"] += int(c.total_cents)
        entry["consumptions"].append(c)
    return list(groups.values())


# ---------------------------------------------------------------------------
# invoice creation
# ---------------------------------------------------------------------------
def _next_invoice_number(session: Session, prefix: str, period: date) -> str:
    """Globally unique, human readable and collision free.

    Uses the existing numbers of that day and picks the first free suffix, so
    deleted invoices can never cause a UNIQUE violation.
    """
    day = period.strftime("%Y%m%d")
    existing = {
        row[0]
        for row in session.execute(
            select(Invoice.invoice_number).where(Invoice.invoice_number.like(f"{prefix}{day}-%"))
        ).all()
    }
    for counter in range(1, 100000):
        candidate = f"{prefix}{day}-{counter:04d}"
        if candidate not in existing:
            return candidate
    raise BillingError("Es konnte keine freie Rechnungsnummer ermittelt werden")


def _existing_invoice(
    session: Session, person_id: int, period: date
) -> Invoice | None:
    """Latest non-cancelled invoice of a person for a period."""
    stmt = (
        select(Invoice)
        .where(
            Invoice.person_id == person_id,
            Invoice.period_date == period,
            Invoice.status != InvoiceStatus.STORNIERT,
        )
        .order_by(Invoice.sequence.desc(), Invoice.id.desc())
    )
    return session.execute(stmt).scalars().first()


def _next_sequence(session: Session, person_id: int, period: date) -> int:
    """Next invoice sequence for a person/day.

    A person may legitimately receive several invoices on the same day
    (manual billing, then the automatic run, ...), so the sequence is a
    running counter and never reused.
    """
    current = session.execute(
        select(func.coalesce(func.max(Invoice.sequence), 0)).where(
            Invoice.person_id == person_id,
            Invoice.period_date == period,
        )
    ).scalar_one()
    return int(current or 0) + 1


def _pending_invoices(session: Session, person_id: int) -> list[Invoice]:
    """Every invoice of this person that was created but never delivered.

    Deliberately **not** limited to the current period: a failed invoice keeps
    its bookings open until it really went out, so it must be retried even
    days later. Oldest first, so the chronology stays intact.
    """
    stmt = (
        select(Invoice)
        .where(
            Invoice.person_id == person_id,
            Invoice.status.in_([InvoiceStatus.OFFEN, InvoiceStatus.FEHLGESCHLAGEN]),
        )
        .order_by(Invoice.period_date, Invoice.sequence, Invoice.id)
    )
    return list(session.execute(stmt).scalars())


def build_invoice_payload(
    *,
    settings: Settings,
    person: Person,
    items: list[dict],
    total_cents: int,
    period_date: date,
    invoice_number: str,
    credit_applied_cents: int = 0,
    balance_cents: int = 0,
    created_at: datetime | None = None,
) -> tuple[str, str, str, str]:
    """Return ``(paypal_link, subject, text_body, html_body)``.

    The invoice is a statement of consumption that is **already** booked on the
    credit account. It therefore never charges anything again. ``credit_applied``
    is the part of the total that existing credit covered when the bookings were
    entered; the remainder is what actually has to be paid.

    A payment link is only produced for a positive remainder. A bill fully
    covered by credit gets no link at all, and no PayPal username is required in
    that case.
    """
    currency = settings.currency
    credit = max(0, min(int(credit_applied_cents), int(total_cents)))
    due = int(total_cents) - credit

    if total_cents < 0:
        raise MoneyError("Betrag darf nicht negativ sein")
    if total_cents == 0:
        raise MoneyError("Betrag ist 0 - es wird keine E-Mail versendet")

    link = ""
    if due > 0:
        username = settings.paypal_username
        if not username:
            raise PayPalLinkError(
                "Kein PayPal.Me-Benutzername konfiguriert. Bitte unter "
                "Einstellungen -> PayPal hinterlegen."
            )
        link = build_link(username, due, currency, settings.paypal_base_url)
    else:
        # Vollstaendig durch Guthaben gedeckt: kein Link, kein Handle noetig.
        username = settings.paypal_username or ""
        log.info(
            "Rechnung %s ist durch Guthaben gedeckt (Verzehr %s, Guthaben %s)",
            invoice_number,
            total_cents,
            credit,
        )

    rendered = et.render(
        person=person,
        items=items,
        total_cents=total_cents,
        currency=currency,
        paypal_link=link,
        paypal_username=username,
        invoice_number=invoice_number,
        period_date=period_date,
        app_name=settings.app_name,
        subject_template=settings.get(EMAIL_SUBJECT_TEMPLATE) or "",
        text_template=settings.get(EMAIL_BODY_TEXT_TEMPLATE) or "",
        html_template=settings.get(EMAIL_BODY_HTML_TEMPLATE) or "",
        date_format=settings.date_format,
        created_at=created_at,
        credit_applied_cents=credit,
        amount_due_cents=due,
        balance_cents=int(balance_cents),
    )
    if rendered.missing:
        log.warning(
            "Unbekannte Platzhalter in der E-Mail-Vorlage: %s", ", ".join(rendered.missing)
        )
    html_doc = et.wrap_html_document(
        body=rendered.html,
        subject=rendered.subject,
        app_name=settings.app_name,
        invoice_number=invoice_number,
        period=period_date.strftime(settings.date_format),
    )
    return link, rendered.subject, rendered.text, html_doc


def create_invoice(
    session: Session,
    settings: Settings,
    person: Person,
    consumptions: list[Consumption],
    period_date: date,
    *,
    trigger: str = "automatic",
    created_by: str | None = None,
    sequence: int = 1,
    commit: bool = True,
) -> tuple[Invoice, list[dict]]:
    items = group_consumptions(consumptions)
    total_cents = sum(int(i["total_cents"]) for i in items)
    currency = settings.currency

    # Das Konto wird hier nur AUSGELESEN, nie belastet. Der Verzehr ist bereits
    # bei der Erfassung gebucht; credit_applied dokumentiert, welcher Teil davon
    # durch damaliges Guthaben gedeckt war.
    credit_applied, amount_due = invoice_credit_and_due(session, consumptions)
    # Snapshot aus den Buchungen selbst lesen, damit eine frisch erstellte und
    # eine rueckuebernommene Rechnung identisch rechnen.
    balance_before, balance_after = invoice_balance_snapshot(consumptions)
    current_balance = account_balance_cents(session, person.id)

    prefix = settings.get(INVOICE_NUMBER_PREFIX) or "RE-"
    invoice_number = _next_invoice_number(session, prefix, period_date)

    link, subject, text_body, html_body = build_invoice_payload(
        settings=settings,
        person=person,
        items=items,
        total_cents=total_cents,
        period_date=period_date,
        invoice_number=invoice_number,
        credit_applied_cents=credit_applied,
        balance_cents=current_balance,
    )

    invoice = Invoice(
        invoice_number=invoice_number,
        person_id=person.id,
        period_date=period_date,
        sequence=sequence,
        total_cents=total_cents,
        currency=currency,
        status=InvoiceStatus.OFFEN,
        payment_status=PaymentStatus.OFFEN,
        paypal_link=link or None,
        paypal_username=settings.paypal_username or None,
        email_subject=subject,
        email_body=text_body,
        email_html=html_body,
        is_automatic=trigger == "automatic",
        created_by=created_by,
        credit_applied_cents=credit_applied,
        amount_due_cents=amount_due,
        balance_before_cents=balance_before,
        balance_after_cents=balance_after,
    )
    session.add(invoice)
    session.flush()
    note_invoice_issued(session, invoice)

    for position, item in enumerate(items):
        session.add(
            InvoiceItem(
                invoice_id=invoice.id,
                product_name=item["product_name"],
                unit_price_cents=item["unit_price_cents"],
                quantity=item["quantity"],
                total_cents=item["total_cents"],
                position=position,
            )
        )
        for c in item["consumptions"]:
            c.invoice_id = invoice.id
            c.status = ConsumptionStatus.ABGERECHNET
            c.invoiced_at = c.invoiced_at or utcnow()

    if commit:
        session.commit()
    else:
        session.flush()
    return invoice, items


# ---------------------------------------------------------------------------
# sending
# ---------------------------------------------------------------------------
def send_invoice(
    session: Session,
    settings: Settings,
    invoice: Invoice,
    *,
    mailer: Mailer | None = None,
    retry_attempts: int = 2,
    retry_delay: float = 1.0,
    commit: bool = True,
) -> SendResult:
    """Send the invoice mail.

    The invoice is only marked as successfully billed after the SMTP server
    accepted the message. On failure the invoice stays retryable.
    """
    person = session.get(Person, invoice.person_id)
    assert person is not None

    invoice.send_attempts = int(invoice.send_attempts or 0) + 1

    result = SendResult(ok=False, error="nicht versendet")
    for attempt in range(1, max(1, retry_attempts) + 1):
        active = mailer or build_mailer(settings, get_store())
        # re-validate the link right before sending
        try:
            link = build_link(
                settings.paypal_username,
                int(invoice.total_cents),
                invoice.currency,
                settings.paypal_base_url,
            )
        except (PayPalLinkError, MoneyError) as exc:
            result = SendResult(ok=False, error=str(exc))
            break
        invoice.paypal_link = link

        result = active.send(
            to=person.email,
            subject=invoice.email_subject or settings.app_name,
            text_body=invoice.email_body or "",
            html_body=invoice.email_html,
        )
        if result.ok:
            break
        log.warning(
            "Versand fehlgeschlagen (Versuch %s/%s) fuer %s: %s",
            attempt,
            retry_attempts,
            invoice.invoice_number,
            result.error,
        )
        if attempt < retry_attempts and retry_delay > 0:
            time.sleep(retry_delay * attempt)

    if result.ok:
        invoice.status = InvoiceStatus.VERSENDT
        invoice.sent_at = utcnow()
        invoice.last_error = None
        if invoice.payment_status == PaymentStatus.OFFEN:
            invoice.payment_status = PaymentStatus.ZAHLUNG_ANGEFORDERT
        for c in invoice.consumptions:
            c.status = ConsumptionStatus.ABGERECHNET
            if c.invoiced_at is None:
                c.invoiced_at = utcnow()
        log.info(
            "Abrechnung %s an %s versendet (%s Cent)",
            invoice.invoice_number,
            person.email,
            invoice.total_cents,
        )
    else:
        invoice.status = InvoiceStatus.FEHLGESCHLAGEN
        invoice.last_error = result.error[:1000]
        # The bookings stay OFFEN - and stay linked to this invoice - so a retry
        # can mark exactly these rows as billed. They still count towards the
        # open balance until the mail really went out.
        for c in list(invoice.consumptions):
            c.status = ConsumptionStatus.OFFEN
            c.invoiced_at = None

    if commit:
        session.commit()
    return result


def mark_invoice_paid(
    session: Session,
    invoice: Invoice,
    *,
    amount_cents: int | None = None,
    commit: bool = True,
) -> Invoice:
    """Manual "paid" marking. Never triggered by the system automatically."""
    invoice.payment_status = PaymentStatus.BEZAHLT
    invoice.paid_at = utcnow()
    invoice.paid_amount_cents = (
        amount_cents if amount_cents is not None else invoice.total_cents
    )
    if commit:
        session.commit()
    return invoice


def cancel_invoice(session: Session, invoice: Invoice, *, commit: bool = True) -> Invoice:
    """Cancel an invoice and release its bookings back to OFFEN.

    The bookings keep the credit they consumed when they were entered, because
    the balance already reflects it. A cancelled invoice therefore reports no
    credit of its own - the next invoice that picks the bookings up reports the
    same figure, so it is never counted twice.
    """
    was_open = invoice.status != InvoiceStatus.STORNIERT
    for c in invoice.consumptions:
        c.status = ConsumptionStatus.OFFEN
        c.invoice_id = None
        c.invoiced_at = None
    invoice.status = InvoiceStatus.STORNIERT
    invoice.payment_status = PaymentStatus.STORNIERT
    invoice.credit_applied_cents = 0
    invoice.amount_due_cents = 0
    if was_open:
        account = get_or_create_account(session, invoice.person, currency=invoice.currency)
        account.total_invoiced_cents = max(
            0, int(account.total_invoiced_cents) - int(invoice.total_cents)
        )
    if commit:
        session.commit()
    return invoice


def delete_invoice(session: Session, invoice: Invoice, *, commit: bool = True) -> None:
    session.delete(invoice)
    if commit:
        session.commit()


# ---------------------------------------------------------------------------
# daily run
# ---------------------------------------------------------------------------
def run_daily_billing(
    session: Session,
    settings: Settings,
    period_date: date,
    *,
    trigger: str = "automatic",
    person_ids: list[int] | None = None,
    mailer: Mailer | None = None,
    created_by: str | None = None,
    retry_attempts: int = 2,
    retry_delay: float = 1.0,
    is_catchup: bool = False,
    commit: bool = True,
) -> BillingResult:
    """Bill all open consumptions up to the end of ``period_date``.

    One invoice per person **per run**. A person may receive several invoices
    on the same day; the ``sequence`` column counts them. An invoice that was
    created but never delivered is retried first, then whatever is still
    unassigned becomes a fresh invoice.
    """
    tz = get_tz(settings.timezone)
    _, end_utc = local_day_bounds(period_date, tz)
    result = BillingResult(period_date=period_date, trigger=trigger, is_catchup=is_catchup)

    run = BillingRun(
        period_date=period_date,
        trigger=trigger,
        is_catchup=is_catchup,
        status="running",
    )
    session.add(run)
    session.flush()
    result.run_id = run.id

    try:
        targets = (
            person_ids
            if person_ids is not None
            else persons_with_open_consumptions(session, end_utc)
        )
        result.persons_count = len(targets)

        if not is_email_configured(settings):
            result.fatal_error = (
                "SMTP ist nicht konfiguriert. Bitte unter Einstellungen -> E-Mail "
                "SMTP-Server und Absenderadresse hinterlegen."
            )
            run.status = "failed"
            run.message = result.fatal_error
            run.finished_at = utcnow()
            if commit:
                session.commit()
            return result

        for person_id in targets:
            entry = PersonBillingResult(person_id=person_id, person_name="", email="")
            person = session.get(Person, person_id)
            if person is None:
                entry.status = "uebersprungen"
                entry.skipped_reason = "Person existiert nicht mehr"
                result.items.append(entry)
                continue
            entry.person_name = person.full_name
            entry.email = person.email

            # 1) Retry every invoice of this person that never made it out.
            #    Their bookings are still OFFEN and still attached, so a
            #    successful retry closes exactly the amount originally invoiced.
            retried_any = False
            delivery_broken = False
            for pending in _pending_invoices(session, person_id):
                if int(pending.total_cents) <= 0:
                    continue
                retried_any = True
                pending_result = send_invoice(
                    session,
                    settings,
                    pending,
                    mailer=mailer,
                    retry_attempts=retry_attempts,
                    retry_delay=retry_delay,
                    commit=False,
                )
                retry_entry = PersonBillingResult(
                    person_id=person_id,
                    person_name=person.full_name,
                    email=person.email,
                    invoice_id=pending.id,
                    invoice_number=pending.invoice_number,
                    total_cents=int(pending.total_cents),
                    paypal_link=pending.paypal_link or "",
                )
                if pending_result.ok:
                    retry_entry.status = "versendet"
                    result.emails_sent += 1
                    result.total_cents += int(pending.total_cents)
                else:
                    retry_entry.status = "fehlgeschlagen"
                    retry_entry.error = pending_result.error
                    result.emails_failed += 1
                    delivery_broken = True
                result.items.append(retry_entry)
                if delivery_broken:
                    # Delivery is broken - do not pile up further invoices.
                    break

            if delivery_broken:
                # Delivery is broken - do not pile up further invoices.
                continue

            # 2) Everything still unassigned becomes a NEW invoice. Multiple
            #    invoices per person and day are allowed on purpose.
            consumptions = [
                c
                for c in open_consumptions_for_person(
                    session, person_id, end_utc, unassigned_only=True
                )
                if int(c.total_cents) > 0
            ]
            if not consumptions:
                if not retried_any:
                    entry.status = "uebersprungen"
                    entry.skipped_reason = "keine offenen Buchungen"
                    result.items.append(entry)
                continue

            try:
                invoice, _items = create_invoice(
                    session,
                    settings,
                    person,
                    consumptions,
                    period_date,
                    trigger=trigger,
                    created_by=created_by,
                    sequence=_next_sequence(session, person_id, period_date),
                    commit=False,
                )
                result.invoices_created += 1
            except (PayPalLinkError, MoneyError, ValueError) as exc:
                session.rollback()
                entry.status = "fehlgeschlagen"
                entry.error = str(exc)
                result.emails_failed += 1
                result.items.append(entry)
                continue

            entry.invoice_id = invoice.id
            entry.invoice_number = invoice.invoice_number
            entry.total_cents = invoice.total_cents
            entry.paypal_link = invoice.paypal_link or ""

            send_result = send_invoice(
                session,
                settings,
                invoice,
                mailer=mailer,
                retry_attempts=retry_attempts,
                retry_delay=retry_delay,
                commit=False,
            )
            if send_result.ok:
                entry.status = "versendet"
                result.emails_sent += 1
                result.total_cents += int(invoice.total_cents)
            else:
                entry.status = "fehlgeschlagen"
                entry.error = send_result.error
                result.emails_failed += 1
            result.items.append(entry)

        run.persons_count = result.persons_count
        run.invoices_created = result.invoices_created
        run.emails_sent = result.emails_sent
        run.emails_failed = result.emails_failed
        run.total_cents = result.total_cents
        if result.fatal_error:
            run.status = "failed"
        elif result.emails_failed:
            run.status = "partial"
        else:
            run.status = "success"
        run.message = result.fatal_error or None
        run.finished_at = utcnow()
        if commit:
            session.commit()
        return result
    except Exception as exc:  # noqa: BLE001 - must not kill the scheduler
        session.rollback()
        log.exception("Tagesabrechnung fehlgeschlagen")
        result.fatal_error = f"{type(exc).__name__}: {exc}"[:500]
        run = session.get(BillingRun, result.run_id)
        if run is not None:
            run.status = "failed"
            run.message = result.fatal_error
            run.finished_at = utcnow()
            if commit:
                session.commit()
        return result


def send_pending_invoices(
    session: Session,
    settings: Settings,
    *,
    mailer: Mailer | None = None,
    invoice_ids: list[int] | None = None,
    retry_attempts: int = 2,
    retry_delay: float = 1.0,
    commit: bool = True,
) -> list[tuple[Invoice, SendResult]]:
    """Retry every invoice that was never delivered successfully."""
    stmt = select(Invoice).options(joinedload(Invoice.person)).where(
        Invoice.status.in_([InvoiceStatus.OFFEN, InvoiceStatus.FEHLGESCHLAGEN]),
        Invoice.payment_status != PaymentStatus.STORNIERT,
        Invoice.total_cents > 0,
    )
    if invoice_ids is not None:
        stmt = stmt.where(Invoice.id.in_(invoice_ids))
    stmt = stmt.order_by(Invoice.id.asc())

    out: list[tuple[Invoice, SendResult]] = []
    for invoice in session.execute(stmt).scalars().unique().all():
        result = send_invoice(
            session,
            settings,
            invoice,
            mailer=mailer,
            retry_attempts=retry_attempts,
            retry_delay=retry_delay,
            commit=False,
        )
        out.append((invoice, result))
    if commit:
        session.commit()
    return out


# ---------------------------------------------------------------------------
# catch-up
# ---------------------------------------------------------------------------
def periods_due(
    settings: Settings, last_billed: date | None, now: datetime | None = None
) -> list[date]:
    """Which periods still need billing.

    Returns the days between the last successful run and the most recent
    billable day, so a server that was down at 17:00 catches up instead of
    silently skipping a day.
    """
    if not settings.auto_billing_enabled:
        return []
    tz = get_tz(settings.timezone)
    now = now or local_now(tz)
    today = now.date()
    hour, minute = (
        int(part) for part in (settings.auto_billing_time or "17:00").split(":")
    )
    scheduled = datetime.combine(today, dtime(hour, minute), tzinfo=tz)
    latest = today if now >= scheduled else today - timedelta(days=1)

    if last_billed is None:
        # never run before: only the current period
        return [latest]

    periods: list[date] = []
    cursor = last_billed + timedelta(days=1)
    guard = 0
    while cursor <= latest and guard < 400:
        periods.append(cursor)
        cursor += timedelta(days=1)
        guard += 1
    return periods


def next_run_at(settings: Settings, now: datetime | None = None) -> datetime:
    tz = get_tz(settings.timezone)
    now = now or local_now(tz)
    hour, minute = (
        int(part) for part in (settings.auto_billing_time or "17:00").split(":")
    )
    scheduled = datetime.combine(now.date(), dtime(hour, minute), tzinfo=tz)
    if now >= scheduled:
        scheduled = datetime.combine(
            now.date() + timedelta(days=1), dtime(hour, minute), tzinfo=tz
        )
    return scheduled
