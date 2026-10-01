"""Credit accounts: balance, ledger and the data backfill for existing records.

Rules implemented here, in one place so no caller can break them:

* A booking (consumption) hits the balance **immediately**, at the moment it is
  entered. Invoicing never charges again - an invoice only states how much of
  its consumption was already covered by credit.
* A deposit credits the balance.
* The ledger is append only. A wrong booking is balanced with a separate
  ``KORREKTUR`` entry, never by editing or deleting rows.
* Amounts are integer cents throughout. No floating point is used for money.

Idempotency is enforced twice: every ledger row carries either a
``consumption_id`` or a ``deposit_id``, and both columns are UNIQUE in the
schema. Calling :func:`post_consumption` or :func:`post_deposit` twice for the
same source object is therefore a no-op instead of a double booking.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import (
    Account,
    Consumption,
    ConsumptionStatus,
    Deposit,
    DepositEmailStatus,
    Invoice,
    InvoiceStatus,
    LedgerEntry,
    LedgerEntryType,
    PaymentType,
    Person,
    utcnow,
)

log = logging.getLogger(__name__)

# Marker in app_meta so the backfill is a no-op after its first successful run.
BACKFILL_KEY = "accounts_backfill_version"
BACKFILL_VERSION = "1"


# ---------------------------------------------------------------------------
# Konto holen / anlegen
# ---------------------------------------------------------------------------
def get_account(session: Session, person_id: int) -> Account | None:
    return session.execute(
        select(Account).where(Account.person_id == person_id)
    ).scalar_one_or_none()


def get_or_create_account(
    session: Session, person: Person, *, currency: str = "EUR"
) -> Account:
    """Return the account of *person*, creating an empty one if needed.

    Every existing and every newly created person therefore always has an
    account, without a separate onboarding step.
    """
    account = get_account(session, person.id)
    if account is not None:
        return account
    account = Account(
        person_id=person.id,
        currency=currency or "EUR",
        balance_cents=0,
        total_consumption_cents=0,
        total_deposit_cents=0,
        total_invoiced_cents=0,
    )
    session.add(account)
    session.flush()
    return account


def balance_cents(session: Session, person_id: int) -> int:
    account = get_account(session, person_id)
    return int(account.balance_cents) if account else 0


# ---------------------------------------------------------------------------
# Interne Helfer
# ---------------------------------------------------------------------------
def credit_available(balance: int, amount_cents: int) -> int:
    """How much of *amount_cents* existing credit already covers.

    Used at booking time, so the resulting figure can be summed per invoice to
    obtain the credit that invoice offset - without touching the balance twice.
    """
    if amount_cents <= 0:
        return 0
    return max(0, min(int(balance), int(amount_cents)))


def _append(
    session: Session,
    account: Account,
    *,
    entry_type: LedgerEntryType,
    amount_cents: int,
    consumption: Consumption | None = None,
    deposit: Deposit | None = None,
    invoice: Invoice | None = None,
    reverses: LedgerEntry | None = None,
    credit_applied_cents: int = 0,
    note: str | None = None,
    created_at: datetime | None = None,
) -> LedgerEntry:
    """Append one movement and move the balance with it.

    The caller decides consumption/deposit; at most one of them may be set,
    which keeps the UNIQUE constraints meaningful. A correcting entry instead
    links to the row it balances via ``reverses``, because it must not reuse the
    same ``consumption_id``.
    """
    amount = int(amount_cents)
    before = int(account.balance_cents)
    after = before + amount
    stamp = created_at or utcnow()

    entry = LedgerEntry(
        account_id=account.id,
        person_id=account.person_id,
        entry_type=entry_type,
        amount_cents=amount,
        balance_before_cents=before,
        balance_after_cents=after,
        consumption_id=consumption.id if consumption is not None else None,
        deposit_id=deposit.id if deposit is not None else None,
        invoice_id=invoice.id if invoice is not None else None,
        reverses_entry_id=reverses.id if reverses is not None else None,
        credit_applied_cents=int(credit_applied_cents),
        note=note,
        created_at=stamp,
    )
    session.add(entry)

    account.balance_cents = after
    account.last_entry_at = stamp
    if entry_type == LedgerEntryType.VERZEHR:
        account.total_consumption_cents = int(account.total_consumption_cents) - amount
    elif entry_type == LedgerEntryType.EINZAHLUNG:
        account.total_deposit_cents = int(account.total_deposit_cents) + amount
    return entry


# ---------------------------------------------------------------------------
# Buchung (Verzehr)
# ---------------------------------------------------------------------------
def post_consumption(
    session: Session,
    consumption: Consumption,
    *,
    currency: str = "EUR",
    note: str | None = None,
) -> LedgerEntry | None:
    """Charge a booking to the account right away.

    Returns ``None`` when the booking was already posted, so a repeated call can
    never charge twice. ``credit_applied_cents`` on the booking records how much
    of it existing credit covered at that moment; that figure is what an invoice
    later reports as offset credit.
    """
    existing = session.execute(
        select(LedgerEntry).where(LedgerEntry.consumption_id == consumption.id)
    ).scalar_one_or_none()
    if existing is not None:
        log.debug("Buchung %s war bereits verbucht", consumption.id)
        return None

    account = get_or_create_account(session, consumption.person, currency=currency)
    amount = int(consumption.total_cents)
    covered = credit_available(int(account.balance_cents), amount)

    consumption.balance_before_cents = int(account.balance_cents)
    consumption.credit_applied_cents = covered

    entry = _append(
        session,
        account,
        entry_type=LedgerEntryType.VERZEHR,
        amount_cents=-amount,
        consumption=consumption,
        credit_applied_cents=covered,
        note=note or consumption.note,
        created_at=consumption.created_at,
    )
    session.flush()
    return entry


def _original_entry(session: Session, consumption: Consumption) -> LedgerEntry | None:
    return session.execute(
        select(LedgerEntry).where(LedgerEntry.consumption_id == consumption.id)
    ).scalar_one_or_none()


def _reversal_of(session: Session, entry: LedgerEntry) -> LedgerEntry | None:
    return session.execute(
        select(LedgerEntry).where(LedgerEntry.reverses_entry_id == entry.id)
    ).scalar_one_or_none()


def reverse_consumption(
    session: Session,
    consumption: Consumption,
    *,
    currency: str = "EUR",
    reason: str | None = None,
) -> LedgerEntry | None:
    """Balance a cancelled booking with a compensating ``KORREKTUR`` entry.

    History stays intact: the original ``VERZEHR`` row remains visible next to
    its correction, which points at it via ``reverses_entry_id``. Returns ``None``
    if the booking was never charged or is already reversed, so a repeated
    cancellation can never credit the account twice.
    """
    original = _original_entry(session, consumption)
    if original is None:
        return None
    if _reversal_of(session, original) is not None:
        return None
    account = session.get(Account, original.account_id)
    if account is None:
        return None
    entry = _append(
        session,
        account,
        entry_type=LedgerEntryType.KORREKTUR,
        amount_cents=-int(original.amount_cents),
        reverses=original,
        note=reason or "Storniert",
        created_at=utcnow(),
    )
    session.flush()
    return entry


def reopen_consumption(
    session: Session, consumption: Consumption, *, currency: str = "EUR"
) -> LedgerEntry | None:
    """Re-charge a booking that was cancelled before.

    A second ``VERZEHR`` row would violate the unique ``consumption_id``, so the
    reactivation is booked as ``KORREKTUR`` pointing at the cancellation. Returns
    ``None`` when there is nothing to undo (never charged, or still booked).
    """
    original = _original_entry(session, consumption)
    if original is None:
        return post_consumption(session, consumption, currency=currency)
    reversal = _reversal_of(session, original)
    if reversal is None:
        return None  # still booked, nothing to do
    account = session.get(Account, original.account_id)
    if account is None:
        return None
    entry = _append(
        session,
        account,
        entry_type=LedgerEntryType.KORREKTUR,
        amount_cents=int(original.amount_cents),
        reverses=reversal,
        note="Buchung wieder aktiviert",
        created_at=utcnow(),
    )
    session.flush()
    return entry


# ---------------------------------------------------------------------------
# Einzahlungen
# ---------------------------------------------------------------------------
def post_deposit(
    session: Session,
    person: Person,
    amount_cents: int,
    *,
    payment_type: PaymentType = PaymentType.BAR,
    paid_at: datetime | None = None,
    note: str | None = None,
    created_by: str | None = None,
    currency: str = "EUR",
    send_email: bool = True,
) -> tuple[Deposit, LedgerEntry]:
    """Record a deposit and credit it in one transaction.

    The caller commits. The confirmation mail is sent by
    :mod:`app.services.deposits` *after* the commit, so a failing SMTP server
    can never discard the deposit.
    """
    from ..money import MoneyError  # local import avoids a cycle

    amount = int(amount_cents)
    if amount <= 0:
        raise MoneyError("Einzahlungsbetrag muss groesser als 0 sein")

    account = get_or_create_account(session, person, currency=currency)
    before = int(account.balance_cents)

    deposit = Deposit(
        person_id=person.id,
        amount_cents=amount,
        currency=currency or "EUR",
        payment_type=payment_type,
        paid_at=paid_at or utcnow(),
        note=note,
        created_by=created_by,
        balance_before_cents=before,
        balance_after_cents=before + amount,
        email_status=(
            DepositEmailStatus.OFFEN if send_email else DepositEmailStatus.NICHT_GESENDET
        ),
    )
    session.add(deposit)
    session.flush()

    entry = _append(
        session,
        account,
        entry_type=LedgerEntryType.EINZAHLUNG,
        amount_cents=amount,
        deposit=deposit,
        note=note,
        created_at=deposit.paid_at,
    )
    deposit.balance_after_cents = int(account.balance_cents)
    session.flush()
    return deposit, entry


def adjust_balance(
    session: Session,
    person: Person,
    amount_cents: int,
    *,
    reason: str,
    currency: str = "EUR",
    created_by: str | None = None,
) -> LedgerEntry:
    """Administative correction, used for opening balances.

    This is the documented way to fix a starting balance by hand. It never
    touches a consumption or a deposit, so it cannot collide with the unique
    constraints, and it stays visible in the history forever.
    """
    account = get_or_create_account(session, person, currency=currency)
    entry = _append(
        session,
        account,
        entry_type=LedgerEntryType.KORREKTUR,
        amount_cents=int(amount_cents),
        note=reason,
        created_at=utcnow(),
    )
    session.flush()
    return entry


def set_opening_balance(
    session: Session,
    person: Person,
    target_cents: int,
    *,
    reason: str = "Anfangsbestand manuell korrigiert",
    currency: str = "EUR",
) -> LedgerEntry | None:
    """Move the balance to *target_cents* with one visible correction.

    Implemented as a delta so the whole history is preserved. Returns ``None``
    when the balance already matches, in which case no ledger row is created.
    This is the documented way to compensate for payments that were never
    stored - the system never invents those amounts itself.
    """
    account = get_or_create_account(session, person, currency=currency)
    delta = int(target_cents) - int(account.balance_cents)
    if delta == 0:
        log.info("Anfangsbestand fuer Person %s ist bereits korrekt", person.id)
        return None
    entry = _append(
        session,
        account,
        entry_type=LedgerEntryType.KORREKTUR,
        amount_cents=delta,
        note=reason,
        created_at=utcnow(),
    )
    session.flush()
    return entry


# ---------------------------------------------------------------------------
# Auswertungen
# ---------------------------------------------------------------------------
def invoice_credit_and_due(session: Session, consumptions) -> tuple[int, int]:
    """Credit offset by *consumptions* and the amount still payable.

    ``credit`` is the sum of the credit each booking consumed when it was
    entered. Because that credit was already spent on the balance back then,
    invoicing must not book anything again - it only reports the split.
    """
    credit = 0
    total = 0
    for row in consumptions:
        total += int(row.total_cents)
        credit += int(row.credit_applied_cents or 0)
    credit = max(0, min(credit, total))
    return credit, total - credit


def summary(session: Session, person_id: int) -> dict:
    """Everything the overview and the account page need."""
    account = get_account(session, person_id)
    open_consumption = int(
        session.execute(
            select(func.coalesce(func.sum(Consumption.total_cents), 0)).where(
                Consumption.person_id == person_id,
                Consumption.status == ConsumptionStatus.OFFEN,
            )
        ).scalar_one()
        or 0
    )
    last_invoice = session.execute(
        select(Invoice)
        .where(
            Invoice.person_id == person_id,
            Invoice.status != InvoiceStatus.STORNIERT,
        )
        .order_by(Invoice.period_date.desc(), Invoice.sequence.desc(), Invoice.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    last_deposit = session.execute(
        select(Deposit)
        .where(Deposit.person_id == person_id)
        .order_by(Deposit.paid_at.desc(), Deposit.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    return {
        "account": account,
        "balance_cents": int(account.balance_cents) if account else 0,
        "total_consumption_cents": int(account.total_consumption_cents) if account else 0,
        "total_deposit_cents": int(account.total_deposit_cents) if account else 0,
        "total_invoiced_cents": int(account.total_invoiced_cents) if account else 0,
        "open_consumption_cents": open_consumption,
        "last_invoice": last_invoice,
        "last_deposit": last_deposit,
    }


def verify_accounts(session: Session) -> list[dict]:
    """Recompute every balance from the ledger and report deviations.

    The stored balance is written together with its ledger entry, so a correct
    database yields no findings. Used by the tests and available as a safety
    check after a restore.
    """
    findings: list[dict] = []
    accounts = list(session.execute(select(Account)).scalars())
    for account in accounts:
        total = int(
            session.execute(
                select(func.coalesce(func.sum(LedgerEntry.amount_cents), 0)).where(
                    LedgerEntry.account_id == account.id
                )
            ).scalar_one()
            or 0
        )
        if total != int(account.balance_cents):
            findings.append(
                {
                    "person_id": account.person_id,
                    "stored_cents": int(account.balance_cents),
                    "ledger_cents": total,
                }
            )
        last = session.execute(
            select(LedgerEntry.balance_after_cents)
            .where(LedgerEntry.account_id == account.id)
            .order_by(LedgerEntry.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        if last is not None and int(last) != int(account.balance_cents):
            findings.append(
                {
                    "person_id": account.person_id,
                    "last_entry_cents": int(last),
                    "stored_cents": int(account.balance_cents),
                }
            )
    return findings


# ---------------------------------------------------------------------------
# Rueckuebernahme vorhandener Daten
# ---------------------------------------------------------------------------
def invoice_balance_snapshot(
    bookings: Sequence[Consumption],
) -> tuple[int, int]:
    """Return ``(balance_before, balance_after)`` for a set of bookings.

    Both values are read back from the bookings, never re-derived from the
    current account balance:

    * ``balance_before`` is the balance in front of the **oldest** booking,
    * ``balance_after`` is that balance reduced by the full amount of **all**
      bookings.

    A booking stores the balance in front of itself and consumes credit out of
    that same balance, so the credit must not be added on top - doing so would
    push the snapshot further and further up.

    This is the single source of truth for the snapshot: :func:`create_invoice`
    and :func:`_backfill_invoices` both call it, so a freshly invoiced period and
    a migrated one cannot disagree.
    """
    if not bookings:
        return 0, 0
    ordered = sorted(bookings, key=lambda c: (c.created_at, c.id or 0))
    opening = int(ordered[0].balance_before_cents or 0)
    return opening, opening - sum(int(b.total_cents) for b in ordered)


def _backfill_invoices(session: Session, account: Account) -> dict:
    """Fill the invoice snapshots that migration 003 added.

    ``credit_applied_cents`` and ``amount_due_cents`` are derived from the
    bookings, which already know how much credit each of them consumed when it
    was entered. The balance snapshots describe the moment of invoicing:
    *before* is the balance in front of the oldest booking of the invoice,
    *after* the balance behind the newest one. Both are read back from the
    bookings, so no amount is estimated.

    Cancelled invoices carry no money and stay at zero.
    """
    stats = {"invoices_updated": 0, "invoiced_cents": 0}
    invoices = list(
        session.execute(
            select(Invoice).where(Invoice.person_id == account.person_id).order_by(Invoice.id)
        ).scalars()
    )
    for invoice in invoices:
        if invoice.status == InvoiceStatus.STORNIERT:
            invoice.credit_applied_cents = 0
            invoice.amount_due_cents = 0
            continue
        rows = list(
            session.execute(
                select(Consumption)
                .where(Consumption.invoice_id == invoice.id)
                .order_by(Consumption.created_at, Consumption.id)
            ).scalars()
        )
        if rows:
            credit, due = invoice_credit_and_due(session, rows)
            before, after = invoice_balance_snapshot(rows)
            invoice.balance_before_cents = before
            invoice.balance_after_cents = after
        else:
            # Rechnung ohne verknuepfte Buchungen: nur aus dem Rechnungskopf.
            credit = 0
            due = int(invoice.total_cents)
        invoice.credit_applied_cents = credit
        invoice.amount_due_cents = due
        stats["invoices_updated"] += 1
        stats["invoiced_cents"] += int(invoice.total_cents)
    account.total_invoiced_cents = stats["invoiced_cents"]
    return stats


def note_invoice_issued(session: Session, invoice: Invoice) -> None:
    """Add a newly created invoice to the account's invoiced total.

    Invoicing moves no credit; this only keeps the running totals on the account
    complete, so the overview can show them without re-reading every invoice.
    """
    if invoice.status == InvoiceStatus.STORNIERT:
        return
    account = get_or_create_account(session, invoice.person, currency=invoice.currency)
    account.total_invoiced_cents = int(account.total_invoiced_cents) + int(invoice.total_cents)


def ensure_accounts(
    session: Session, *, currency: str = "EUR", force: bool = False
) -> dict:
    """Give every person an account and derive their balance from real data.

    Called on startup, so a database that predates migration 003 is carried over
    without any manual step. It is safe to call repeatedly:

    * the marker in ``app_meta`` turns later calls into a single read,
    * accounts are created only where missing,
    * a booking becomes a ledger row only if it has none (the UNIQUE constraint
      on ``consumption_id`` is the final guard),
    * running balances are replayed in chronological order,
    * nothing is ever deleted.

    No amounts are invented. Payments that were never stored stay invisible; the
    administrator sets those by hand via :func:`set_opening_balance`.
    """
    stats = {
        "accounts_created": 0,
        "entries_created": 0,
        "skipped_existing": 0,
        "invoices_updated": 0,
        "persons": 0,
        "already_done": False,
    }

    if not force and backfill_status(session) == BACKFILL_VERSION:
        stats["already_done"] = True
        return stats

    persons = list(session.execute(select(Person).order_by(Person.id)).scalars())
    for person in persons:
        had_account = get_account(session, person.id) is not None
        account = get_or_create_account(session, person, currency=currency)
        if not had_account:
            stats["accounts_created"] += 1
        stats["persons"] += 1

        # Existing accounts are authoritative once they carry history; only a
        # freshly created account needs its balance derived from the bookings.
        already_posted = int(
            session.execute(
                select(func.count())
                .select_from(LedgerEntry)
                .where(LedgerEntry.account_id == account.id)
            ).scalar_one()
            or 0
        )
        if not already_posted:
            # Deposits and bookings are replayed together in chronological
            # order. Deposits must take part: they are the credit the bookings
            # consumed, and skipping them would derive a balance that is too low
            # and a credit_applied of zero.
            events: list[tuple[datetime, int, int, str, object]] = []
            for dep in session.execute(
                select(Deposit).where(Deposit.person_id == person.id)
            ).scalars():
                events.append(
                    (dep.paid_at, 0, dep.id, "deposit", dep)
                )
            for booking in session.execute(
                select(Consumption).where(Consumption.person_id == person.id)
            ).scalars():
                events.append(
                    (booking.created_at, 1, booking.id, "booking", booking)
                )
            events.sort(key=lambda e: (e[0], e[1], e[2]))

            for stamp, _kind, _ident, what, row in events:
                if what == "deposit":
                    dep = row
                    assert isinstance(dep, Deposit)
                    amount = int(dep.amount_cents)
                    dep.balance_before_cents = int(account.balance_cents)
                    dep.balance_after_cents = int(account.balance_cents) + amount
                    _append(
                        session,
                        account,
                        entry_type=LedgerEntryType.EINZAHLUNG,
                        amount_cents=amount,
                        deposit=dep,
                        note=dep.note,
                        created_at=stamp,
                    )
                    stats["entries_created"] += 1
                    continue

                booking = row
                assert isinstance(booking, Consumption)
                amount = int(booking.total_cents)
                if booking.status == ConsumptionStatus.STORNIERT:
                    # A cancelled booking never charged the account.
                    booking.balance_before_cents = int(account.balance_cents)
                    booking.credit_applied_cents = 0
                    continue
                balance = int(account.balance_cents)
                covered = credit_available(balance, amount)
                booking.balance_before_cents = balance
                booking.credit_applied_cents = covered
                _append(
                    session,
                    account,
                    entry_type=LedgerEntryType.VERZEHR,
                    amount_cents=-amount,
                    consumption=booking,
                    credit_applied_cents=covered,
                    note=booking.note,
                    created_at=stamp,
                )
                stats["entries_created"] += 1
            session.flush()
        else:
            stats["skipped_existing"] += 1

        stats["invoices_updated"] += _backfill_invoices(session, account)["invoices_updated"]

    _mark_backfilled(session)
    session.commit()
    return stats


def _mark_backfilled(session: Session) -> None:
    from ..models import AppMeta

    row = session.get(AppMeta, BACKFILL_KEY)
    if row is None:
        session.add(AppMeta(key=BACKFILL_KEY, value=BACKFILL_VERSION))
    else:
        row.value = BACKFILL_VERSION


def backfill_status(session: Session) -> str | None:
    from ..models import AppMeta

    row = session.get(AppMeta, BACKFILL_KEY)
    return row.value if row else None


__all__ = [
    "BACKFILL_KEY",
    "BACKFILL_VERSION",
    "Account",
    "adjust_balance",
    "backfill_status",
    "balance_cents",
    "credit_available",
    "ensure_accounts",
    "get_account",
    "get_or_create_account",
    "invoice_credit_and_due",
    "note_invoice_issued",
    "post_consumption",
    "post_deposit",
    "reopen_consumption",
    "reverse_consumption",
    "set_opening_balance",
    "summary",
    "verify_accounts",
]