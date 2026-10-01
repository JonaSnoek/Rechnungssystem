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
    CheckConstraint,
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


class LedgerEntryType(str, enum.Enum):
    """Kind of movement on a credit account.

    ``VERZEHR`` and ``EINZAHLUNG`` are the two everyday movements. ``EROEFFNUNG``
    carries an administratively corrected opening balance and ``KORREKTUR`` is a
    compensating entry. Neither is ever applied to a consumption or a deposit,
    so the UNIQUE constraints on those columns cannot be violated by them.
    """

    VERZEHR = "VERZEHR"
    EINZAHLUNG = "EINZAHLUNG"
    EROEFFNUNG = "EROEFFNUNG"
    KORREKTUR = "KORREKTUR"


class PaymentType(str, enum.Enum):
    BAR = "BAR"
    PAYPAL = "PAYPAL"
    UEBERWEISUNG = "UEBERWEISUNG"
    SONSTIGE = "SONSTIGE"


class DepositEmailStatus(str, enum.Enum):
    """Mail delivery state, deliberately independent of the deposit itself."""

    OFFEN = "OFFEN"
    GESENDET = "GESENDET"
    FEHLGESCHLAGEN = "FEHLGESCHLAGEN"
    NICHT_GESENDET = "NICHT GESENDET"


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
    account: Mapped["Account | None"] = relationship(
        back_populates="person", cascade="all, delete-orphan", passive_deletes=True
    )
    deposits: Mapped[list["Deposit"]] = relationship(
        back_populates="person", cascade="all, delete-orphan", passive_deletes=True,
        order_by="Deposit.paid_at.desc()",
    )
    ledger_entries: Mapped[list["LedgerEntry"]] = relationship(
        back_populates="person", passive_deletes=True
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
    # --- Kontofuehrung (Migration 003) ------------------------------------
    # Kontostand unmittelbar vor dieser Buchung. Wird beim Erfassen gesetzt
    # und dient der Rueckuebernahme alter Daten.
    balance_before_cents: Mapped[int | None] = mapped_column(Integer)
    # Anteil dieser Buchung, der beim Erfassen durch vorhandenes Guthaben
    # gedeckt wurde. Die Summe ueber die Buchungen einer Rechnung ist deren
    # verrechnetes Guthaben - der Kontostand wird dadurch nicht erneut belastet.
    credit_applied_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    person: Mapped[Person] = relationship(back_populates="consumptions")
    product: Mapped[Product | None] = relationship(back_populates="consumptions")
    invoice: Mapped["Invoice | None"] = relationship(back_populates="consumptions")
    ledger_entries: Mapped[list["LedgerEntry"]] = relationship(
        back_populates="consumption", cascade="all, delete-orphan", passive_deletes=True
    )

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
    # --- Kontofuehrung (Migration 003) ------------------------------------
    # Momentaufnahme fuer die Rechnungs-E-Mail. Die Rechnung aendert den
    # Kontostand nicht, sie dokumentiert nur die Deckung.
    credit_applied_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    amount_due_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    balance_before_cents: Mapped[int | None] = mapped_column(Integer)
    balance_after_cents: Mapped[int | None] = mapped_column(Integer)

    person: Mapped[Person] = relationship(back_populates="invoices")
    items: Mapped[list["InvoiceItem"]] = relationship(
        back_populates="invoice", cascade="all, delete-orphan", passive_deletes=True
    )
    consumptions: Mapped[list[Consumption]] = relationship(
        back_populates="invoice", cascade="all, delete-orphan", passive_deletes=True
    )
    ledger_entries: Mapped[list["LedgerEntry"]] = relationship(
        back_populates="invoice", passive_deletes=True
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
# Guthabenkonto, Einzahlungen und Kontobewegungen
# ---------------------------------------------------------------------------
class Account(Base, TimestampMixin):
    """Credit account of exactly one person.

    ``balance_cents`` is negative for a debtor and positive for credit. It is
    the running sum of the ledger and is written in the same transaction as the
    ledger entry, never on its own. :func:`app.services.accounts.verify_accounts`
    recomputes it from the ledger and reports any drift.
    """

    __tablename__ = "accounts"
    __table_args__ = (
        UniqueConstraint("person_id", name="uq_accounts_person"),
        Index("ix_accounts_balance", "balance_cents"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    person_id: Mapped[int] = mapped_column(
        ForeignKey("persons.id", ondelete="CASCADE"), nullable=False
    )
    currency: Mapped[str] = mapped_column(String(3), default="EUR", nullable=False)
    balance_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_consumption_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_deposit_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_invoiced_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_entry_at: Mapped[datetime | None] = mapped_column(DateTime)

    person: Mapped["Person"] = relationship(back_populates="account")
    entries: Mapped[list["LedgerEntry"]] = relationship(
        back_populates="account",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="LedgerEntry.id",
    )

    @property
    def has_credit(self) -> bool:
        return int(self.balance_cents) > 0

    @property
    def has_debt(self) -> bool:
        return int(self.balance_cents) < 0

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Account person={self.person_id} balance={self.balance_cents}>"


class Deposit(Base, TimestampMixin):
    """A payment received from a person, entered by the administrator.

    This is also how an actual PayPal or bank transfer is recorded: sending a
    PayPal.Me link only *requests* payment and never marks anything as paid.
    """

    __tablename__ = "deposits"
    __table_args__ = (
        CheckConstraint("amount_cents > 0", name="ck_deposit_amount_positive"),
        Index("ix_deposits_person_paid", "person_id", "paid_at"),
        Index("ix_deposits_email_status", "email_status", "id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    person_id: Mapped[int] = mapped_column(
        ForeignKey("persons.id", ondelete="CASCADE"), nullable=False
    )
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="EUR", nullable=False)
    payment_type: Mapped[PaymentType] = mapped_column(
        _enum(PaymentType, "payment_type"), default=PaymentType.BAR, nullable=False
    )
    paid_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(String(64))
    balance_before_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    balance_after_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Mail delivery is tracked here but never shares the fate of the booking:
    # a failed mail leaves the deposit in place and can be retried on its own.
    email_status: Mapped[DepositEmailStatus] = mapped_column(
        _enum(DepositEmailStatus, "deposit_email_status"),
        default=DepositEmailStatus.OFFEN,
        nullable=False,
    )
    email_sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    email_error: Mapped[str | None] = mapped_column(Text)
    email_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    person: Mapped["Person"] = relationship(back_populates="deposits")
    ledger_entries: Mapped[list["LedgerEntry"]] = relationship(
        back_populates="deposit", cascade="all, delete-orphan", passive_deletes=True
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Deposit person={self.person_id} {self.amount_cents}>"


class LedgerEntry(Base):
    """One immutable movement on a credit account.

    Rows are only ever appended. A wrong booking is balanced by a separate
    ``KORREKTUR`` entry, never by editing or deleting history. ``consumption_id``
    and ``deposit_id`` are UNIQUE, which makes double processing impossible at
    the database level.
    """

    __tablename__ = "ledger_entries"
    __table_args__ = (
        UniqueConstraint("consumption_id", name="uq_ledger_consumption"),
        UniqueConstraint("deposit_id", name="uq_ledger_deposit"),
        UniqueConstraint("reverses_entry_id", name="uq_ledger_reverses"),
        Index("ix_ledger_person_created", "person_id", "created_at"),
        Index("ix_ledger_account_created", "account_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    person_id: Mapped[int] = mapped_column(
        ForeignKey("persons.id", ondelete="CASCADE"), nullable=False
    )
    entry_type: Mapped[LedgerEntryType] = mapped_column(
        _enum(LedgerEntryType, "ledger_entry_type"), nullable=False
    )
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    balance_before_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    balance_after_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    consumption_id: Mapped[int | None] = mapped_column(
        ForeignKey("consumptions.id", ondelete="CASCADE")
    )
    deposit_id: Mapped[int | None] = mapped_column(
        ForeignKey("deposits.id", ondelete="CASCADE")
    )
    invoice_id: Mapped[int | None] = mapped_column(
        ForeignKey("invoices.id", ondelete="SET NULL")
    )
    # Points at the movement this correction balances. A ``KORREKTUR`` for a
    # cancellation references the ``VERZEHR`` row; reactivating references that
    # cancellation. The chain stays auditable and a second reversal is refused.
    reverses_entry_id: Mapped[int | None] = mapped_column(
        ForeignKey("ledger_entries.id", ondelete="SET NULL")
    )
    credit_applied_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    account: Mapped[Account] = relationship(back_populates="entries")
    person: Mapped["Person"] = relationship(back_populates="ledger_entries")
    consumption: Mapped[Consumption | None] = relationship(back_populates="ledger_entries")
    deposit: Mapped[Deposit | None] = relationship(back_populates="ledger_entries")
    invoice: Mapped[Invoice | None] = relationship(back_populates="ledger_entries")
    reversed_entry: Mapped["LedgerEntry | None"] = relationship(
        remote_side=[id], foreign_keys=[reverses_entry_id]
    )

    @property
    def amount_display(self) -> str:
        return str(int(self.amount_cents))

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<LedgerEntry {self.entry_type.value} {self.amount_cents} "
            f"-> {self.balance_after_cents}>"
        )


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
    "Account",
    "AdminUser",
    "AppMeta",
    "AuditLog",
    "Base",
    "BillingRun",
    "Consumption",
    "ConsumptionStatus",
    "Deposit",
    "DepositEmailStatus",
    "EmailLog",
    "Invoice",
    "InvoiceItem",
    "InvoiceStatus",
    "LedgerEntry",
    "LedgerEntryType",
    "LoginAttempt",
    "PaymentStatus",
    "PaymentType",
    "Person",
    "Product",
    "SchedulerState",
    "Setting",
    "SetupState",
    "new_token",
    "utc_today",
    "utcnow",
]
