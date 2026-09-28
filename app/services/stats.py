"""Dashboard and statistics queries."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone as _timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import (
    Consumption,
    ConsumptionStatus,
    Invoice,
    InvoiceStatus,
    PaymentStatus,
    Person,
    Product,
)
from ..settings_service import Settings
from .billing import get_tz, local_day_bounds, local_now, next_run_at, open_balance_cents_bulk


@dataclass
class DashboardStats:
    open_persons: int = 0
    open_total_cents: int = 0
    open_items: int = 0
    today_consumed_items: int = 0
    today_consumed_cents: int = 0
    today_bookings: int = 0
    today_billed_persons: int = 0
    today_billed_cents: int = 0
    today_billed_items: int = 0
    open_invoices: int = 0
    failed_invoices: int = 0
    unpaid_invoices: int = 0
    unpaid_cents: int = 0
    active_persons: int = 0
    active_products: int = 0
    next_run_at: datetime | None = None
    timezone: str = "Europe/Berlin"
    billing_time: str = "17:00"
    auto_billing_enabled: bool = True
    setup_completed: bool = False
    today: date = field(default_factory=lambda: date.today())


@dataclass
class ActivityEntry:
    created_at: datetime
    person_name: str
    summary: str
    total_cents: int
    currency: str
    actor: str | None = None


def dashboard_stats(session: Session, settings: Settings) -> DashboardStats:
    tz = get_tz(settings.timezone)
    now = local_now(tz)
    today = now.date()
    start_utc, end_utc = local_day_bounds(today, tz)
    currency = settings.currency

    stats = DashboardStats(
        timezone=settings.timezone,
        billing_time=settings.auto_billing_time,
        auto_billing_enabled=settings.auto_billing_enabled,
        setup_completed=settings.setup_completed,
        today=today,
    )

    balances = open_balance_cents_bulk(session)
    stats.open_persons = len(balances)
    stats.open_total_cents = sum(balances.values())

    stats.open_items = int(
        session.execute(
            select(func.coalesce(func.sum(Consumption.quantity), 0)).where(
                Consumption.status == ConsumptionStatus.OFFEN
            )
        ).scalar_one()
        or 0
    )

    today_rows = session.execute(
        select(
            func.coalesce(func.sum(Consumption.quantity), 0),
            func.coalesce(func.sum(Consumption.total_cents), 0),
            func.count(Consumption.id),
        ).where(Consumption.created_at >= start_utc, Consumption.created_at < end_utc)
    ).one()
    stats.today_consumed_items = int(today_rows[0] or 0)
    stats.today_consumed_cents = int(today_rows[1] or 0)
    stats.today_bookings = int(today_rows[2] or 0)

    billed = session.execute(
        select(
            func.count(Invoice.id),
            func.coalesce(func.sum(Invoice.total_cents), 0),
        ).where(
            Invoice.period_date == today,
            Invoice.status == InvoiceStatus.VERSENDT,
        )
    ).one()
    stats.today_billed_persons = int(billed[0] or 0)
    stats.today_billed_cents = int(billed[1] or 0)

    stats.today_billed_items = int(
        session.execute(
            select(func.coalesce(func.sum(Consumption.quantity), 0))
            .join(Invoice, Consumption.invoice_id == Invoice.id)
            .where(
                Invoice.period_date == today,
                Invoice.status == InvoiceStatus.VERSENDT,
            )
        ).scalar_one()
        or 0
    )

    stats.open_invoices = int(
        session.execute(
            select(func.count(Invoice.id)).where(
                Invoice.status.in_([InvoiceStatus.OFFEN, InvoiceStatus.FEHLGESCHLAGEN])
            )
        ).scalar_one()
        or 0
    )
    stats.failed_invoices = int(
        session.execute(
            select(func.count(Invoice.id)).where(
                Invoice.status == InvoiceStatus.FEHLGESCHLAGEN
            )
        ).scalar_one()
        or 0
    )

    unpaid = session.execute(
        select(func.count(Invoice.id), func.coalesce(func.sum(Invoice.total_cents), 0)).where(
            Invoice.payment_status.in_(
                [PaymentStatus.OFFEN, PaymentStatus.ZAHLUNG_ANGEFORDERT]
            ),
            Invoice.status != InvoiceStatus.STORNIERT,
        )
    ).one()
    stats.unpaid_invoices = int(unpaid[0] or 0)
    stats.unpaid_cents = int(unpaid[1] or 0)

    stats.active_persons = int(
        session.execute(
            select(func.count(Person.id)).where(Person.is_active.is_(True))
        ).scalar_one()
        or 0
    )
    stats.active_products = int(
        session.execute(
            select(func.count(Product.id)).where(Product.is_active.is_(True))
        ).scalar_one()
        or 0
    )

    stats.next_run_at = (
        next_run_at(settings, now) if settings.auto_billing_enabled else None
    )
    return stats


def recent_activity(session: Session, settings: Settings, limit: int = 15) -> list[ActivityEntry]:
    """Consumption bookings and billing actions, newest first."""
    tz = get_tz(settings.timezone)
    currency = settings.currency
    rows = session.execute(
        select(Consumption, Person.first_name, Person.last_name)
        .join(Person, Consumption.person_id == Person.id)
        .order_by(Consumption.created_at.desc(), Consumption.id.desc())
        .limit(limit)
    ).all()
    return [
        ActivityEntry(
            created_at=row[0].created_at,
            person_name=f"{row[1]} {row[2]}",
            summary=f"+ {row[0].quantity}\u00d7 {row[0].product_name}",
            total_cents=int(row[0].total_cents),
            currency=row[0].currency or currency,
        )
        for row in rows
    ]


def open_persons_with_balance(session: Session) -> list[tuple[Person, int, int]]:
    """Persons with an open amount, sorted by amount descending."""
    balances = open_balance_cents_bulk(session)
    if not balances:
        return []
    persons = (
        session.execute(
            select(Person).where(Person.id.in_(list(balances.keys())))
        )
        .scalars()
        .all()
    )
    counts: dict[int, int] = {}
    rows = session.execute(
        select(Consumption.person_id, func.sum(Consumption.quantity))
        .where(
            Consumption.person_id.in_(list(balances.keys())),
            Consumption.status == ConsumptionStatus.OFFEN,
        )
        .group_by(Consumption.person_id)
    ).all()
    for pid, qty in rows:
        counts[int(pid)] = int(qty or 0)
    result = [(p, balances[p.id], counts.get(p.id, 0)) for p in persons]
    result.sort(key=lambda item: (-item[1], item[0].last_name.lower()))
    return result


def person_history(
    session: Session, person_id: int, limit: int = 100
) -> tuple[list[Consumption], list[Invoice]]:
    consumptions = list(
        session.execute(
            select(Consumption)
            .where(Consumption.person_id == person_id)
            .order_by(Consumption.created_at.desc(), Consumption.id.desc())
            .limit(limit)
        )
        .scalars()
        .all()
    )
    invoices = list(
        session.execute(
            select(Invoice)
            .where(Invoice.person_id == person_id)
            .order_by(Invoice.period_date.desc(), Invoice.id.desc())
            .limit(limit)
        )
        .scalars()
        .all()
    )
    return consumptions, invoices


def consumption_trend(session: Session, settings: Settings, days: int = 14) -> list[dict]:
    tz = get_tz(settings.timezone)
    today = local_now(tz).date()
    start = today - timedelta(days=days - 1)
    start_utc, _ = local_day_bounds(start, tz)
    rows = session.execute(
        select(Consumption.created_at, Consumption.total_cents, Consumption.quantity)
        .where(Consumption.created_at >= start_utc)
        .order_by(Consumption.created_at.asc())
    ).all()
    buckets = {
        (start + timedelta(days=offset)).isoformat(): {"date": start + timedelta(days=offset), "cents": 0, "items": 0}
        for offset in range(days)
    }
    for created_at, total, qty in rows:
        key = created_at.replace(tzinfo=_timezone.utc).astimezone(tz).date().isoformat()
        if key in buckets:
            buckets[key]["cents"] += int(total or 0)
            buckets[key]["items"] += int(qty or 0)
    return list(buckets.values())
