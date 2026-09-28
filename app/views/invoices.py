"""Abrechnungen: automatische Tagesabrechnung, manuelle Abrechnung, Historie."""

from __future__ import annotations

import logging
from datetime import date

from flask import (
    Blueprint,
    Response,
    flash,
    g,
    redirect,
    render_template,
    request,
    url_for,
)
from sqlalchemy import func, or_, select

from ..models import (
    BillingRun,
    Consumption,
    ConsumptionStatus,
    EmailLog,
    Invoice,
    InvoiceStatus,
    PaymentStatus,
    Person,
    utcnow,
)
from ..money import format_cents
from ..security import audit, login_required, setup_required, validate_csrf
from ..services.billing import (
    cancel_invoice,
    get_tz,
    local_now,
    mark_invoice_paid,
    periods_due,
    run_daily_billing,
    send_invoice,
    send_pending_invoices,
)
from ..services.export import invoices_csv
from ..settings_service import Settings

log = logging.getLogger(__name__)

bp = Blueprint("invoices", __name__, url_prefix="/abrechnungen")


@bp.route("/")
@setup_required
@login_required
def index():
    db = g.db
    settings = Settings(db)
    currency = settings.currency
    tz = get_tz(settings.timezone)
    now = local_now(tz)

    status_filter = (request.args.get("status") or "").strip()
    payment_filter = (request.args.get("payment") or "").strip()
    query = (request.args.get("q") or "").strip()
    page = max(1, request.args.get("page", type=int) or 1)
    per_page = 25

    stmt = select(Invoice, Person).join(Person, Invoice.person_id == Person.id)
    if status_filter in {s.value for s in InvoiceStatus}:
        stmt = stmt.where(Invoice.status == InvoiceStatus(status_filter))
    if payment_filter in {s.value for s in PaymentStatus}:
        stmt = stmt.where(Invoice.payment_status == PaymentStatus(payment_filter))
    if query:
        like = f"%{query}%"
        stmt = stmt.where(
            or_(
                Invoice.invoice_number.ilike(like),
                Person.first_name.ilike(like),
                Person.last_name.ilike(like),
                Person.email.ilike(like),
            )
        )
    count_stmt = select(func.count()).select_from(stmt.subquery())
    total = int(db.execute(count_stmt).scalar_one() or 0)
    rows = db.execute(
        stmt.order_by(Invoice.period_date.desc(), Invoice.id.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
    ).all()

    runs = list(
        db.execute(select(BillingRun).order_by(BillingRun.id.desc()).limit(10)).scalars().all()
    )
    pending = int(
        db.execute(
            select(func.count(Invoice.id)).where(
                Invoice.status.in_([InvoiceStatus.OFFEN, InvoiceStatus.FEHLGESCHLAGEN])
            )
        ).scalar_one()
        or 0
    )
    open_amount = int(
        db.execute(
            select(func.coalesce(func.sum(Consumption.total_cents), 0)).where(
                Consumption.status == ConsumptionStatus.OFFEN
            )
        ).scalar_one()
        or 0
    )
    due = periods_due(settings, _last_billed(db))

    return render_template(
        "invoices/index.html",
        rows=rows,
        runs=runs,
        total=total,
        page=page,
        pages=max(1, (total + per_page - 1) // per_page),
        per_page=per_page,
        status_filter=status_filter,
        payment_filter=payment_filter,
        query=query,
        pending=pending,
        open_amount=open_amount,
        due=due,
        currency=currency,
        now=now,
        InvoiceStatus=InvoiceStatus,
        PaymentStatus=PaymentStatus,
    )


def _open_balance(db, person_id: int) -> int:
    return int(
        db.execute(
            select(func.coalesce(func.sum(Consumption.total_cents), 0)).where(
                Consumption.person_id == person_id,
                Consumption.status == ConsumptionStatus.OFFEN,
            )
        ).scalar_one()
        or 0
    )


def _last_billed(db) -> date | None:
    from ..models import SchedulerState

    state = db.get(SchedulerState, 1)
    return state.last_billed_period if state else None


@bp.route("/<int:invoice_id>")
@setup_required
@login_required
def detail(invoice_id: int):
    db = g.db
    settings = Settings(db)
    invoice = db.get(Invoice, invoice_id)
    if invoice is None:
        flash("Abrechnung nicht gefunden.", "error")
        return redirect(url_for("invoices.index"))
    person = db.get(Person, invoice.person_id)
    email_logs = list(
        db.execute(
            select(EmailLog).where(EmailLog.invoice_id == invoice.id).order_by(EmailLog.id.desc())
        )
        .scalars()
        .all()
    )
    return render_template(
        "invoices/detail.html",
        invoice=invoice,
        person=person,
        items=invoice.items,
        email_logs=email_logs,
        currency=settings.currency,
        InvoiceStatus=InvoiceStatus,
        PaymentStatus=PaymentStatus,
    )


@bp.route("/<int:invoice_id>/erneut-senden", methods=["POST"])
@setup_required
@login_required
def resend(invoice_id: int):
    validate_csrf()
    db = g.db
    settings = Settings(db)
    invoice = db.get(Invoice, invoice_id)
    if invoice is None:
        flash("Abrechnung nicht gefunden.", "error")
        return redirect(url_for("invoices.index"))
    result = send_invoice(db, settings, invoice, retry_attempts=2, retry_delay=1.0)
    if result.ok:
        flash(f"Abrechnung {invoice.invoice_number} wurde erneut versendet.", "success")
    else:
        flash(
            f"Versand fehlgeschlagen: {result.error}. Die Buchungen bleiben offen.",
            "error",
        )
    audit(db, "invoice.resent", target=invoice.invoice_number, detail=result.error or "ok")
    return redirect(url_for("invoices.detail", invoice_id=invoice.id))


@bp.route("/<int:invoice_id>/bezahlt", methods=["POST"])
@setup_required
@login_required
def mark_paid(invoice_id: int):
    """Manual payment confirmation. Never automatic - no PayPal API in v1."""
    validate_csrf()
    db = g.db
    invoice = db.get(Invoice, invoice_id)
    if invoice is None:
        flash("Abrechnung nicht gefunden.", "error")
        return redirect(url_for("invoices.index"))
    if invoice.status == InvoiceStatus.STORNIERT:
        flash("Stornierte Abrechnung kann nicht bezahlt markiert werden.", "warning")
        return redirect(url_for("invoices.detail", invoice_id=invoice.id))
    mark_invoice_paid(db, invoice)
    audit(db, "invoice.paid", target=invoice.invoice_number, detail=invoice.paypal_link or "")
    flash(f"{invoice.invoice_number} wurde als bezahlt markiert.", "success")
    return redirect(request.referrer or url_for("invoices.detail", invoice_id=invoice.id))


@bp.route("/<int:invoice_id>/zahlung-offen", methods=["POST"])
@setup_required
@login_required
def mark_unpaid(invoice_id: int):
    validate_csrf()
    db = g.db
    invoice = db.get(Invoice, invoice_id)
    if invoice is None:
        flash("Abrechnung nicht gefunden.", "error")
        return redirect(url_for("invoices.index"))
    invoice.payment_status = PaymentStatus.ZAHLUNG_ANGEFORDERT
    invoice.paid_at = None
    invoice.paid_amount_cents = None
    db.commit()
    audit(db, "invoice.unpaid", target=invoice.invoice_number)
    flash("Zahlungsstatus zurueckgesetzt.", "success")
    return redirect(request.referrer or url_for("invoices.detail", invoice_id=invoice.id))


@bp.route("/<int:invoice_id>/stornieren", methods=["POST"])
@setup_required
@login_required
def cancel(invoice_id: int):
    validate_csrf()
    db = g.db
    invoice = db.get(Invoice, invoice_id)
    if invoice is None:
        flash("Abrechnung nicht gefunden.", "error")
        return redirect(url_for("invoices.index"))
    if invoice.status == InvoiceStatus.STORNIERT:
        flash("Abrechnung ist bereits storniert.", "warning")
        return redirect(url_for("invoices.detail", invoice_id=invoice.id))
    cancel_invoice(db, invoice)
    audit(db, "invoice.cancelled", target=invoice.invoice_number)
    flash(
        f"{invoice.invoice_number} wurde storniert. Die Buchungen sind wieder offen.",
        "success",
    )
    return redirect(url_for("invoices.detail", invoice_id=invoice.id))


@bp.route("/<int:invoice_id>/loeschen", methods=["POST"])
@setup_required
@login_required
def delete(invoice_id: int):
    validate_csrf()
    db = g.db
    invoice = db.get(Invoice, invoice_id)
    if invoice is None:
        flash("Abrechnung nicht gefunden.", "error")
        return redirect(url_for("invoices.index"))
    number = invoice.invoice_number
    from ..services.billing import delete_invoice

    delete_invoice(db, invoice)
    audit(db, "invoice.deleted", target=number)
    flash(f"{number} wurde geloescht.", "success")
    return redirect(url_for("invoices.index"))


@bp.route("/person/<int:person_id>", methods=["GET", "POST"])
@setup_required
@login_required
def person_billing(person_id: int):
    """Manual single-person billing page."""
    validate_csrf()
    db = g.db
    settings = Settings(db)
    person = db.get(Person, person_id)
    if person is None:
        flash("Person nicht gefunden.", "error")
        return redirect(url_for("persons.index"))

    if request.method == "POST":
        tz = get_tz(settings.timezone)
        today = local_now(tz).date()
        result = _bill_person(db, settings, person, today, g.admin.username)
        audit(
            db,
            "invoice.manual_person",
            target=person.full_name,
            detail=f"{result.emails_sent} versendet / {result.emails_failed} fehlgeschlagen",
        )
        new_balance = _open_balance(db, person_id)
        if result.emails_sent and not result.emails_failed:
            flash(
                f"Rechnung erfolgreich versendet. Abgerechnet: "
                f"{format_cents(result.total_cents, settings.currency)}. "
                f"Neuer offener Betrag: {format_cents(new_balance, settings.currency)}.",
                "success",
            )
        elif result.emails_sent:
            flash(
                f"Rechnung versendet, {result.emails_failed} fehlgeschlagen. "
                "Fehlgeschlagene Buchungen bleiben offen.",
                "warning",
            )
        elif result.emails_failed or result.fatal_error:
            flash(
                f"Rechnung konnte nicht versendet werden: "
                f"{result.fatal_error or result.first_error() or 'SMTP-Fehler'}. "
                f"Der Betrag bleibt offen "
                f"({format_cents(new_balance, settings.currency)}) - bitte erneut senden.",
                "error",
            )
        else:
            flash("Es gab nichts zu abrechnen.", "info")
        return redirect(url_for("invoices.person_billing", person_id=person_id))

    balance = _open_balance(db, person_id)
    open_bookings = list(
        db.execute(
            select(Consumption)
            .where(
                Consumption.person_id == person_id,
                Consumption.status == ConsumptionStatus.OFFEN,
            )
            .order_by(Consumption.created_at.asc())
        )
        .scalars()
        .all()
    )
    history = list(
        db.execute(
            select(Invoice)
            .where(Invoice.person_id == person_id)
            .order_by(Invoice.period_date.desc(), Invoice.id.desc())
            .limit(20)
        )
        .scalars()
        .all()
    )
    return render_template(
        "invoices/person.html",
        person=person,
        balance=balance,
        open_bookings=open_bookings,
        history=history,
        currency=settings.currency,
    )


def _bill_person(db, settings, person: Person, period: date, actor: str):
    """Bill everything currently unassigned for one person.

    Works exactly like the automatic run: a pending invoice is retried first,
    then any still-open bookings become a new invoice. No time-of-day rule is
    involved - the only criterion is "not yet assigned to a delivered invoice".
    """
    return run_daily_billing(
        db,
        settings,
        period,
        trigger="manual",
        person_ids=[person.id],
        created_by=actor,
        retry_attempts=2,
        retry_delay=1.0,
    )


@bp.route("/jetzt", methods=["POST"])
@setup_required
@login_required
def run_now():
    """Manual daily billing for all persons."""
    validate_csrf()
    db = g.db
    settings = Settings(db)
    tz = get_tz(settings.timezone)
    today = local_now(tz).date()

    if request.form.get("action") == "retry_failed":
        outcomes = send_pending_invoices(db, settings, retry_attempts=2, retry_delay=1.0)
        sent = sum(1 for _, r in outcomes if r.ok)
        failed = len(outcomes) - sent
        audit(db, "billing.retry", detail=f"{sent} ok / {failed} fehlgeschlagen")
        if sent and not failed:
            flash(f"{sent} fehlgeschlagene Abrechnung(en) erfolgreich versendet.", "success")
        elif sent and failed:
            flash(f"{sent} versendet, {failed} weiterhin fehlgeschlagen.", "warning")
        else:
            flash("Es gab keine fehlgeschlagenen Abrechnungen.", "info")
        return redirect(url_for("invoices.index"))

    result = run_daily_billing(
        db,
        settings,
        today,
        trigger="manual",
        created_by=g.admin.username,
        retry_attempts=2,
        retry_delay=1.0,
    )
    audit(
        db,
        "billing.manual",
        detail=f"{result.emails_sent} versendet / {result.emails_failed} fehlgeschlagen",
    )
    if result.fatal_error:
        flash(result.fatal_error, "error")
    elif result.emails_sent and not result.emails_failed:
        flash(
            f"Tagesabrechnung fuer {today.strftime(settings.date_format)} abgeschlossen: "
            f"{result.emails_sent} E-Mail(s) versendet.",
            "success",
        )
    elif result.emails_sent:
        flash(
            f"{result.emails_sent} E-Mail(s) versendet, {result.emails_failed} fehlgeschlagen. "
            f"Fehlgeschlagene Buchungen bleiben offen. Grund: {result.first_error()}",
            "warning",
        )
    elif result.emails_failed:
        flash(
            f"Alle {result.emails_failed} Abrechnungen sind fehlgeschlagen: "
            f"{result.first_error()}. "
            "Buchungen bleiben offen - bitte SMTP pruefen und erneut senden.",
            "error",
        )
    else:
        flash("Es gab keine offenen Buchungen.", "info")
    return redirect(url_for("invoices.index"))


@bp.route("/verpasst", methods=["POST"])
@setup_required
@login_required
def catchup():
    """Bill every period the scheduler considers outstanding."""
    validate_csrf()
    db = g.db
    settings = Settings(db)
    due = periods_due(settings, _last_billed(db))
    if not due:
        flash("Es sind keine Abrechnungen offen.", "info")
        return redirect(url_for("invoices.index"))
    from ..models import SchedulerState

    totals = {"sent": 0, "failed": 0, "invoices": 0}
    for period in due:
        result = run_daily_billing(
            db, settings, period, trigger="catchup", is_catchup=True,
            retry_attempts=1, retry_delay=0.0,
        )
        totals["sent"] += result.emails_sent
        totals["failed"] += result.emails_failed
        totals["invoices"] += result.invoices_created
        state = db.get(SchedulerState, 1)
        if state is not None and not result.fatal_error:
            state.last_billed_period = period
            state.last_success_at = utcnow()
            db.commit()
    audit(db, "billing.catchup", detail=f"{totals['sent']} versendet")
    flash(
        f"Nachholung abgeschlossen: {len(due)} Zeitraum/Traeume, "
        f"{totals['sent']} E-Mail(s) versendet, {totals['failed']} fehlgeschlagen.",
        "success" if not totals["failed"] else "warning",
    )
    return redirect(url_for("invoices.index"))


@bp.route("/export.csv")
@setup_required
@login_required
def export_csv():
    db = g.db
    settings = Settings(db)
    from ..validators import Validator

    v = Validator()
    start = v.date("start", request.args.get("start"))
    end = v.date("end", request.args.get("end"))
    if v.errors:
        flash(v.first_error(), "error")
        return redirect(url_for("invoices.index"))
    data = invoices_csv(db, settings, start=start, end=end)
    audit(db, "invoices.exported", detail=f"{start} bis {end}")
    stamp = local_now(get_tz(settings.timezone)).strftime("%Y%m%d")
    return Response(
        "\ufeff" + data,
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="abrechnungen-{stamp}.csv"'},
    )
