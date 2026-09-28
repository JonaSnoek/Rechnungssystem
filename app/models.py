"""ORM models.

Money is stored as integer cents everywhere. Booking rows keep a *snapshot* of
the unit price and the product name so later price changes never rewrite
history.
"""

from __future__ import annotations

import enum
import secrets as pysecrets
from datetime import date, datetime, timezone

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def utc_today() -> date:
    return utcnow().date()


def new_token(nbytes: int = 24) -> str:
    return pysecrets.token_urlsafe(nbytes)


class PaymentStatus(str, enum.Enum):
    OFFEN = "OFFEN"
    ZAHLUNG_ANGEFORDERT = "ZAHLUNG ANGEFORDERT"
    BEZAHLT = "BEZAHLT"
    STORNIERT = "STORNIERT"


class ConsumptionStatus(str, enum.Enum):
    OFFEN = "OFFEN"
    ABGERECHNET = "ABGERECHNET"
    STORNIERT = "STORNIERT"


class InvoiceStatus(str, enum.Enum):
    OFFEN = "OFFEN"
    VERSENDT = "VERSENDT"
    FEHLGESCHLAGEN = "FEHLGESCHLAGEN"
    STORNIERT = "STORNIERT"


def _enum(py_enum: type[enum.Enum], name: str) -> SAEnum:
    return SAEnum(py_enum, name=name, native_enum=False, length=32, validate_strings=True)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )


# ---------------------------------------------------------------------------
# Administrator
# ---------------------------------------------------------------------------
class AdminUser(Base, TimestampMixin):
    __tablename__ = "admin_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    email: Mapped[str] = mapped_column(String(254), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(120))
    totp_secret: Mapped[str | None] = mapped_column(String(64))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime)
    failed_login_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime)


# ---------------------------------------------------------------------------
# Einstellungen
# ---------------------------------------------------------------------------
class Setting(Base, TimestampMixin):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text)
    # secret values are stored in the local secrets store instead; this flag
    # only marks that a secret exists so the UI can show a masked placeholder
    is_secret: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    group: Mapped[str] = mapped_column(String(40), default="allgemein", nullable=False)


# ---------------------------------------------------------------------------
# Personen
# ---------------------------------------------------------------------------
class Person(Base, TimestampMixin):
    __tablename__ = "persons"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    first_name: Mapped[str] = mapped_column(String(80), nullable=False)
    last_name: Mapped[str] = mapped_column(String(80), nullable=False)
    email: Mapped[str] = mapped_column(String(254), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(40))
    notes: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    consumptions: Mapped[list["Consumption"]] = relationship(
        back_populates="person",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    invoices: Mapped[list["Invoice"]] = relationship(
        back_populates="person", cascade="all, delete-orphan", passive_deletes=True
    )

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

    @property
    def display_name_with_id(self) -> str:
        return f"{self.full_name} (#{self.id})"

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Person {self.id} {self.full_name}>"


# ---------------------------------------------------------------------------
# Produkte
# ---------------------------------------------------------------------------
class Product(Base, TimestampMixin):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    price_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    category: Mapped[str | None] = mapped_column(String(60))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    consumptions: Mapped[list["Consumption"]] = relationship(back_populates="product")

    @property
    def price_changed_at(self) -> datetime | None:
        return self.updated_at

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Product {self.id} {self.name}>"


# ---------------------------------------------------------------------------
# Verzehr / Buchungen
# ---------------------------------------------------------------------------
class Consumption(Base):
    __tablename__ = "consumptions"
    __table_args__ = (
        Index("ix_consumptions_person_status", "person_id", "status"),
        Index("ix_consumptions_created", "created_at"),
        Index("ix_consumptions_status_created", "status", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    person_id: Mapped[int] = mapped_column(
        ForeignKey("persons.id", ondelete="CASCADE"), nullable=False
    )
    product_id: Mapped[int | None] = mapped_column(
        ForeignKey("products.id", ondelete="SET NULL")
    )
    # snapshot -> immune to later product edits / deletions
    product_name: Mapped[str] = mapped_column(String(120), nullable=False)
    product_description: Mapped[str | None] = mapped_column(Text)
    unit_price_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    total_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    currency: Mapped[str] = mapped_column(String(3), default="EUR", nullable=False)
    status: Mapped[ConsumptionStatus] = mapped_column(
        _enum(ConsumptionStatus, "consumption_status"),
        default=ConsumptionStatus.OFFEN,
        nullable=False,
    )
    note: Mapped[str | None] = mapped_column(String(255))
    batch_id: Mapped[str | None] = mapped_column(String(32), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False
    )
    invoiced_at: Mapped[datetime | None] = mapped_column(DateTime)
    invoice_id: Mapped[int | None] = mapped_column(
        ForeignKey("invoices.id", ondelete="CASCADE")
    )

    person: Mapped[Person] = relationship(back_populates="consumptions")
    product: Mapped[Product | None] = relationship(back_populates="consumptions")
    invoice: Mapped["Invoice | None"] = relationship(back_populates="consumptions")

    @property
    def line_total_cents(self) -> int:
        return int(self.unit_price_cents) * int(self.quantity)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Consumption {self.id} {self.quantity}x {self.product_name}>"


# ---------------------------------------------------------------------------
# Abrechnungen
# ---------------------------------------------------------------------------
class Invoice(Base):
    __tablename__ = "invoices"
    __table_args__ = (
        # Guarantees at most one non-cancelled invoice per person per day.
        # This is the hard safety net against duplicate billing.
        UniqueConstraint(
            "person_id",
            "period_date",
            "sequence",
            name="uq_invoice_person_day_sequence",
        ),
        Index("ix_invoices_period_status", "period_date", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    invoice_number: Mapped[str] = mapped_column(
        String(40), unique=True, nullable=False, default=""
    )
    person_id: Mapped[int] = mapped_column(
        ForeignKey("persons.id", ondelete="CASCADE"), nullable=False
    )
    period_date: Mapped[date] = mapped_column(Date, nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    total_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    currency: Mapped[str] = mapped_column(String(3), default="EUR", nullable=False)
    status: Mapped[InvoiceStatus] = mapped_column(
        _enum(InvoiceStatus, "invoice_status"),
        default=InvoiceStatus.OFFEN,
        nullable=False,
    )
    payment_status: Mapped[PaymentStatus] = mapped_column(
        _enum(PaymentStatus, "payment_status"),
        default=PaymentStatus.OFFEN,
        nullable=False,
    )
    paypal_link: Mapped[str | None] = mapped_column(String(300))
    paypal_username: Mapped[str | None] = mapped_column(String(40))
    email_subject: Mapped[str | None] = mapped_column(Text)
    email_body: Mapped[str | None] = mapped_column(Text)
    email_html: Mapped[str | None] = mapped_column(Text)
    send_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime)
    paid_amount_cents: Mapped[int | None] = mapped_column(Integer)
    is_automatic: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_by: Mapped[str | None] = mapped_column(String(64))

    person: Mapped[Person] = relationship(back_populates="invoices")
    items: Mapped[list["InvoiceItem"]] = relationship(
        back_populates="invoice", cascade="all, delete-orphan", passive_deletes=True
    )
    consumptions: Mapped[list[Consumption]] = relationship(
        back_populates="invoice", cascade="all, delete-orphan", passive_deletes=True
    )

    @property
    def is_paid(self) -> bool:
        return self.payment_status == PaymentStatus.BEZAHLT

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Invoice {self.invoice_number} {self.total_cents} {self.currency}>"


class InvoiceItem(Base):
    __tablename__ = "invoice_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    invoice_id: Mapped[int] = mapped_column(
        ForeignKey("invoices.id", ondelete="CASCADE"), nullable=False, index=True
    )
    product_name: Mapped[str] = mapped_column(String(120), nullable=False)
    unit_price_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    total_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    invoice: Mapped[Invoice] = relationship(back_populates="items")


# ---------------------------------------------------------------------------
# Scheduler / Betrieb
# ---------------------------------------------------------------------------
class BillingRun(Base):
    __tablename__ = "billing_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    period_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    trigger: Mapped[str] = mapped_column(String(20), default="automatic", nullable=False)
    is_catchup: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    persons_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    invoices_created: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    emails_sent: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    emails_failed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="running", nullable=False)
    message: Mapped[str | None] = mapped_column(Text)


class SchedulerState(Base):
    __tablename__ = "scheduler_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_error: Mapped[str | None] = mapped_column(Text)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_billed_period: Mapped[date | None] = mapped_column(Date)
    last_tick_at: Mapped[datetime | None] = mapped_column(DateTime)
    heartbeat: Mapped[datetime | None] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )


class LoginAttempt(Base):
    __tablename__ = "login_attempts"
    __table_args__ = (
        Index("ix_login_attempts_identifier_time", "identifier", "attempted_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    identifier: Mapped[str] = mapped_column(String(160), nullable=False)
    ip_address: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    success: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    attempted_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False
    )


class AuditLog(Base):
    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_created", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor: Mapped[str | None] = mapped_column(String(120))
    action: Mapped[str] = mapped_column(String(80), nullable=False)
    target: Mapped[str | None] = mapped_column(String(160))
    detail: Mapped[str | None] = mapped_column(Text)
    ip_address: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False
    )


class EmailLog(Base):
    __tablename__ = "email_log"
    __table_args__ = (Index("ix_email_log_created", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    invoice_id: Mapped[int | None] = mapped_column(
        ForeignKey("invoices.id", ondelete="SET NULL")
    )
    recipient: Mapped[str] = mapped_column(String(254), nullable=False)
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False
    )


class SetupState(Base):
    __tablename__ = "setup_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    completed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
    current_step: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    wizard_data: Mapped[str | None] = mapped_column(Text)


class AppMeta(Base):
    __tablename__ = "app_meta"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )


def next_meta(key: str, session) -> int:  # pragma: no cover - helper
    row = session.get(AppMeta, key)
    current = int(row.value) if row and row.value else 0
    if row is None:
        session.add(AppMeta(key=key, value="1"))
    else:
        row.value = str(current + 1)
    return current + 1


__all__ = [
    "AdminUser",
    "AppMeta",
    "AuditLog",
    "Base",
    "BillingRun",
    "Consumption",
    "ConsumptionStatus",
    "EmailLog",
    "Invoice",
    "InvoiceItem",
    "InvoiceStatus",
    "LoginAttempt",
    "PaymentStatus",
    "Person",
    "Product",
    "SchedulerState",
    "Setting",
    "SetupState",
    "new_token",
    "utc_today",
    "utcnow",
]
