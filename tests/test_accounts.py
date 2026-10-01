"""Guthabenkonten: Belastung, Einzahlungen, Verrechnung und Korrekturen.

Die Tests decken die Regeln ab, die vorher nur in der Beschreibung standen:

* eine Buchung belastet das Konto sofort, nicht erst beim Rechnungsversand,
* eine Rechnung belastet nie erneut, sie weist nur die Verrechnung aus,
* das Ledger haengt nur an; Fehler werden durch Korrekturen ausgeglichen,
* jede Bewegung ist genau einmal gebucht, egal wie oft der Ablauf laeuft,
* E-Mail-Fehler bei einer Einzahlung kippen die Einzahlung nicht.
"""

from __future__ import annotations

import datetime
import pathlib

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.mailer import SendResult
from app.models import (
    Account,
    Consumption,
    ConsumptionStatus,
    Deposit,
    DepositEmailStatus,
    Invoice,
    LedgerEntry,
    LedgerEntryType,
    PaymentType,
    Person,
)
from app.services import accounts as acc
from app.services import billing, deposits as dep_svc
from app.services.billing import run_daily_billing
from app.settings_service import Settings


@pytest.fixture()
def person(db, configured):
    return db.get(Person, configured["person_id"])


def _ledger(db, person_id):
    return (
        db.query(LedgerEntry)
        .filter_by(person_id=person_id)
        .order_by(LedgerEntry.id)
        .all()
    )


def _balance(db, person_id):
    return acc.balance_cents(db, person_id)


# ---------------------------------------------------------------------------
# Buchung belastet sofort
# ---------------------------------------------------------------------------
class TestBuchungBelastetSofort:
    def test_buchung_verringert_kontostand_bei_erfassung(self, db, person):
        row = acc.post_consumption and Consumption(
            person_id=person.id, product_name="Kaffee", unit_price_cents=150,
            quantity=1, total_cents=150, currency="EUR",
            status=ConsumptionStatus.OFFEN,
        )
        db.add(row)
        db.flush()
        acc.post_consumption(db, row)
        db.commit()

        assert _balance(db, person.id) == -150
        entry = _ledger(db, person.id)[0]
        assert entry.entry_type == LedgerEntryType.VERZEHR
        assert entry.amount_cents == -150
        assert entry.balance_before_cents == 0
        assert entry.balance_after_cents == -150

    def test_guthaben_wird_beim_erfassen_verbraucht(self, db, person):
        dep_svc_post(db, person, 1000)
        row = _book(db, person, 400)
        acc.post_consumption(db, row)
        db.commit()

        assert _balance(db, person.id) == 600
        # 400 des Guthabens wurden bei der Erfassung verbraucht.
        assert row.credit_applied_cents == 400
        assert row.balance_before_cents == 1000

    def test_zweite_buchung_nutzt_restguthaben(self, db, person):
        dep_svc_post(db, person, 1000)
        first = _book(db, person, 400)
        acc.post_consumption(db, first)
        db.commit()
        second = _book(db, person, 300)
        acc.post_consumption(db, second)
        db.commit()

        assert first.credit_applied_cents == 400
        assert second.credit_applied_cents == 300
        assert _balance(db, person.id) == 300

    def test_guthaben_reicht_nur_teilweise(self, db, person):
        dep_svc_post(db, person, 200)
        row = _book(db, person, 500)
        acc.post_consumption(db, row)
        db.commit()

        assert row.credit_applied_cents == 200
        assert _balance(db, person.id) == -300

    def test_post_consumption_zweimal_bucht_nur_einmal(self, db, person):
        row = _book(db, person, 250)
        acc.post_consumption(db, row)
        acc.post_consumption(db, row)
        acc.post_consumption(db, row)
        db.commit()

        assert _balance(db, person.id) == -250
        assert len(_ledger(db, person.id)) == 1


def _book(db, person, cents, name="Posten"):
    row = Consumption(
        person_id=person.id, product_name=name, unit_price_cents=cents,
        quantity=1, total_cents=cents, currency="EUR",
        status=ConsumptionStatus.OFFEN,
    )
    db.add(row)
    db.flush()
    return row


def dep_svc_post(db, person, cents, **kw):
    kw.setdefault("send_email", False)
    dep, _entry = acc.post_deposit(db, person, cents, **kw)
    db.commit()
    return dep


def _clear_account_data(db):
    """Kontodaten loeschen, damit ``ensure_accounts(force=True)`` neu aufbaut.

    Einzahlungen bleiben bewusst stehen: die Rueckuebernahme erfindet keine
    Zahlungen, die nie erfasst wurden. Geloescht werden nur Konto und Ledger, die
    aus Einzahlungen und Buchungen wieder abgeleitet werden.
    """
    for model in (LedgerEntry, Account):
        db.query(model).delete()
    db.commit()
    for invoice in db.query(Invoice).all():
        invoice.credit_applied_cents = 0
        invoice.amount_due_cents = 0
        invoice.balance_before_cents = None
        invoice.balance_after_cents = None
    for row in db.query(Consumption).all():
        row.credit_applied_cents = 0
        row.balance_before_cents = None
    db.commit()


# ---------------------------------------------------------------------------
# Rechnung belastet nicht erneut
# ---------------------------------------------------------------------------
class TestRechnungBelastetNicht:
    def test_rechnung_aendert_den_kontostand_nicht(self, db, person, mailer):
        row = _book(db, person, 300)
        acc.post_consumption(db, row)
        db.commit()
        before = _balance(db, person.id)

        billing.create_invoice(db, Settings(db), person, [row],
                               datetime.date(2026, 9, 28), trigger="manual")
        db.commit()

        assert _balance(db, person.id) == before == -300
        # Keine zusaetzliche Bewegung durch die Rechnung.
        assert len(_ledger(db, person.id)) == 1

    def test_rechnung_verrechnet_guthaben_und_rest_bleibt_offen(self, db, person, mailer):
        dep_svc_post(db, person, 1000)
        row = _book(db, person, 600)
        acc.post_consumption(db, row)
        db.commit()

        invoice, _items = billing.create_invoice(
            db, Settings(db), person, [row], datetime.date(2026, 9, 28), trigger="manual"
        )
        db.commit()

        assert invoice.total_cents == 600
        assert invoice.credit_applied_cents == 600
        assert invoice.amount_due_cents == 0
        # Das Konto bleibt bei 400; es wurde nichts zweimal genommen.
        assert _balance(db, person.id) == 400

    def test_teilweise_gedeckte_rechnung_zeigt_restbetrag(self, db, person, mailer):
        dep_svc_post(db, person, 200)
        row = _book(db, person, 500)
        acc.post_consumption(db, row)
        db.commit()

        invoice, _items = billing.create_invoice(
            db, Settings(db), person, [row], datetime.date(2026, 9, 28), trigger="manual"
        )
        db.commit()

        assert invoice.credit_applied_cents == 200
        assert invoice.amount_due_cents == 300
        assert _balance(db, person.id) == -300

    def test_vollstaendig_gedeckte_rechnung_hat_keinen_paypal_link(
        self, db, person, mailer
    ):
        dep_svc_post(db, person, 1000)
        row = _book(db, person, 400)
        acc.post_consumption(db, row)
        db.commit()

        invoice, _items = billing.create_invoice(
            db, Settings(db), person, [row], datetime.date(2026, 9, 28), trigger="manual"
        )
        db.commit()

        assert invoice.amount_due_cents == 0
        assert not invoice.paypal_link
        assert "paypal.me" not in (invoice.email_body or "").lower()

    def test_offene_rechnung_hat_paypal_link(self, db, person, mailer):
        row = _book(db, person, 700)
        acc.post_consumption(db, row)
        db.commit()

        invoice, _items = billing.create_invoice(
            db, Settings(db), person, [row], datetime.date(2026, 9, 28), trigger="manual"
        )
        db.commit()

        assert invoice.amount_due_cents == 700
        assert invoice.paypal_link

    def test_mehrere_buchungen_einer_rechnung_werden_addiert(self, db, person, mailer):
        dep_svc_post(db, person, 500)
        rows = []
        for cents in (200, 300, 400):
            r = _book(db, person, cents)
            acc.post_consumption(db, r)
            rows.append(r)
        db.commit()

        invoice, _items = billing.create_invoice(
            db, Settings(db), person, rows, datetime.date(2026, 9, 28), trigger="manual"
        )
        db.commit()

        assert invoice.total_cents == 900
        assert invoice.credit_applied_cents == 500
        assert invoice.amount_due_cents == 400
        assert _balance(db, person.id) == -400

    def test_stornierte_rechnung_belegt_nicht_wieder(self, db, person, mailer):
        row = _book(db, person, 300)
        acc.post_consumption(db, row)
        db.commit()
        invoice, _items = billing.create_invoice(
            db, Settings(db), person, [row], datetime.date(2026, 9, 28), trigger="manual"
        )
        db.commit()

        billing.cancel_invoice(db, invoice)
        db.commit()

        # Konto unveraendert, keine weitere Bewegung.
        assert _balance(db, person.id) == -300
        assert len(_ledger(db, person.id)) == 1
        assert len(db.query(Invoice).all()) == 1

    def test_snapshot_stimmt_mit_der_buchung_ueberein(self, db, person, mailer):
        """Der Rechnungs-Snapshot wird aus den Buchungen gelesen, nicht geraten.

        Regression: eine frueher hier benutzte Formel (``balance_after - total +
        credit``) addierte das Guthaben doppelt und driftete um den
        Rechnungsbetrag.
        """
        dep_svc_post(db, person, 10000)
        rows = []
        for cents in (2500, 1500):
            r = _book(db, person, cents)
            acc.post_consumption(db, r)
            rows.append(r)
        db.commit()

        invoice, _items = billing.create_invoice(
            db, Settings(db), person, rows, datetime.date(2026, 9, 28), trigger="manual"
        )
        db.commit()

        # Stand vor der aeltesten Buchung: 10000, danach minus beide Buchungen.
        assert invoice.balance_before_cents == 10000
        assert invoice.balance_after_cents == 10000 - 2500 - 1500
        # Kein Aufschlag durch das verrechnete Guthaben.
        assert invoice.balance_before_cents != invoice.balance_after_cents

    def test_rueckuebernahme_und_neue_rechnung_rechnen_gleich(self, db, person, mailer):
        """Migrierte und frisch erstellte Rechnungen muessen identisch rechnen."""
        dep_svc_post(db, person, 10000)
        rows = []
        for cents in (2500, 1500):
            r = _book(db, person, cents)
            acc.post_consumption(db, r)
            rows.append(r)
        db.commit()

        invoice, _items = billing.create_invoice(
            db, Settings(db), person, rows, datetime.date(2026, 9, 28), trigger="manual"
        )
        db.commit()
        frisch = (
            invoice.credit_applied_cents,
            invoice.amount_due_cents,
            invoice.balance_before_cents,
            invoice.balance_after_cents,
        )

        # Alles verwerfen und ueber ensure_accounts neu aufbauen.
        _clear_account_data(db)
        acc.ensure_accounts(db, force=True)
        db.commit()

        alt = db.query(Invoice).one()
        rueck = (
            alt.credit_applied_cents,
            alt.amount_due_cents,
            alt.balance_before_cents,
            alt.balance_after_cents,
        )
        assert rueck == frisch


# ---------------------------------------------------------------------------
# Korrekturen statt Aendern
# ---------------------------------------------------------------------------
class TestKorrekturen:
    def test_storno_goodet_mit_eigener_bewegung(self, db, person):
        row = _book(db, person, 300)
        acc.post_consumption(db, row)
        db.commit()

        acc.reverse_consumption(db, row)
        db.commit()

        assert _balance(db, person.id) == 0
        rows = _ledger(db, person.id)
        assert len(rows) == 2, "Original bleibt stehen, Korrektur kommt dazu"
        assert rows[1].entry_type == LedgerEntryType.KORREKTUR
        assert rows[1].amount_cents == 300
        assert rows[1].reverses_entry_id == rows[0].id

    def test_storno_zweimal_bucht_nicht_wieder(self, db, person):
        row = _book(db, person, 300)
        acc.post_consumption(db, row)
        db.commit()

        acc.reverse_consumption(db, row)
        acc.reverse_consumption(db, row)
        db.commit()

        assert _balance(db, person.id) == 0
        assert len(_ledger(db, person.id)) == 2

    def test_wiederaktivieren_belegt_erneut(self, db, person):
        row = _book(db, person, 300)
        acc.post_consumption(db, row)
        db.commit()
        acc.reverse_consumption(db, row)
        db.commit()

        acc.reopen_consumption(db, row)
        db.commit()

        assert _balance(db, person.id) == -300
        rows = _ledger(db, person.id)
        assert len(rows) == 3
        assert rows[2].reverses_entry_id == rows[1].id

    def test_korrektur_ist_sichtbar_und_append_only(self, db, person):
        row = _book(db, person, 300)
        acc.post_consumption(db, row)
        db.commit()
        original = _ledger(db, person.id)[0]

        acc.adjust_balance(db, person, 100, reason="Testkorrektur")
        db.commit()

        # Der urspruengliche Eintrag ist unveraendert geblieben.
        db.refresh(original)
        assert original.amount_cents == -300
        assert len(_ledger(db, person.id)) == 2

    def test_anfangsbestand_korrigiert_als_sichtbare_buchung(self, db, person):
        acc.set_opening_balance(db, person, 2500, reason="Bargeld vor Kontofuehrung")
        db.commit()

        assert _balance(db, person.id) == 2500
        entries = _ledger(db, person.id)
        assert len(entries) == 1
        assert entries[0].note == "Bargeld vor Kontofuehrung"

    def test_anfangsbestand_der_bereits_stimmt_erzeugt_nichts(self, db, person):
        acc.set_opening_balance(db, person, 500, reason="x")
        db.commit()
        before = len(_ledger(db, person.id))

        result = acc.set_opening_balance(db, person, 500, reason="x")
        db.commit()

        assert result is None
        assert len(_ledger(db, person.id)) == before

    def test_anfangsbestand_kann_schuld_beseitigen(self, db, person):
        row = _book(db, person, 800)
        acc.post_consumption(db, row)
        db.commit()
        assert _balance(db, person.id) == -800

        acc.set_opening_balance(db, person, 0, reason="Verrechnet")
        db.commit()

        assert _balance(db, person.id) == 0


# ---------------------------------------------------------------------------
# Einzahlungen
# ---------------------------------------------------------------------------
class TestEinzahlungen:
    def test_einzahlung_erhoeht_kontostand(self, db, person):
        dep = dep_svc_post(db, person, 2000)
        assert _balance(db, person.id) == 2000
        assert dep.balance_before_cents == 0
        assert dep.balance_after_cents == 2000

        entry = db.query(LedgerEntry).filter_by(deposit_id=dep.id).one()
        assert entry.entry_type == LedgerEntryType.EINZAHLUNG
        assert entry.amount_cents == 2000

    def test_einzahlung_nach_schuld_saldiert(self, db, person):
        row = _book(db, person, 500)
        acc.post_consumption(db, row)
        db.commit()
        assert _balance(db, person.id) == -500

        dep_svc_post(db, person, 300)
        assert _balance(db, person.id) == -200

    def test_betrag_muss_positiv_sein(self, db, person):
        from app.money import MoneyError

        with pytest.raises(MoneyError):
            acc.post_deposit(db, person, 0, send_email=False)
        with pytest.raises(MoneyError):
            acc.post_deposit(db, person, -500, send_email=False)
        db.rollback()
        assert _balance(db, person.id) == 0

    def test_zahlen_sind_integer_cent(self, db, person):
        dep = dep_svc_post(db, person, 1234)
        assert isinstance(dep.amount_cents, int)
        assert _balance(db, person.id) == 1234

    def test_einzahlung_wird_nicht_doppelt_gebucht(self, db, person):
        dep, _entry = acc.post_deposit(db, person, 1000, send_email=False)
        db.commit()
        # Ein Retry des Versands darf das Geld nicht doppeln.
        for _ in range(3):
            dep_svc.send_deposit_confirmation(db, Settings(db), dep, mailer=_ok_mailer())
        db.commit()

        assert _balance(db, person.id) == 1000
        assert db.query(LedgerEntry).filter_by(deposit_id=dep.id).count() == 1


def _ok_mailer():
    class M:
        def send(self, to, subject, text_body, html_body):
            return SendResult(ok=True)

    return M()


def _broken_mailer(error="SMTP nicht erreichbar"):
    class M:
        def send(self, to, subject, text_body, html_body):
            return SendResult(ok=False, error=error)

    return M()


# ---------------------------------------------------------------------------
# E-Mail getrennt von der Buchung
# ---------------------------------------------------------------------------
class TestEinzahlungsMail:
    def test_mail_wird_versendet_und_markiert(self, db, person, mailer):
        dep, _entry = acc.post_deposit(db, person, 500, send_email=True)
        db.commit()
        result = dep_svc.send_deposit_confirmation(db, Settings(db), dep, mailer=mailer)
        db.commit()

        assert result.ok
        assert dep.email_status == DepositEmailStatus.GESENDET
        assert dep.email_sent_at is not None
        assert mailer.sent, "es wurde eine Mail verschickt"

    def test_mailfehler_bucht_die_einzahlung_trotzdem(self, db, person):
        dep, _entry = acc.post_deposit(db, person, 500, send_email=True)
        db.commit()

        result = dep_svc.send_deposit_confirmation(
            db, Settings(db), dep, mailer=_broken_mailer(), retry_delay=0.0
        )
        db.commit()

        assert not result.ok
        assert dep.email_status == DepositEmailStatus.FEHLGESCHLAGEN
        assert dep.email_error
        # Das Geld ist trotzdem gebucht.
        assert _balance(db, person.id) == 500
        assert db.query(LedgerEntry).filter_by(deposit_id=dep.id).count() == 1

    def test_retry_nach_fehler_erhaelt_das_geld(self, db, person, mailer):
        dep, _entry = acc.post_deposit(db, person, 700, send_email=True)
        db.commit()
        dep_svc.send_deposit_confirmation(
            db, Settings(db), dep, mailer=_broken_mailer(), retry_delay=0.0
        )
        db.commit()

        result = dep_svc.send_deposit_confirmation(db, Settings(db), dep, mailer=mailer)
        db.commit()

        assert result.ok
        assert dep.email_status == DepositEmailStatus.GESENDET

    def test_html_bleibt_html_und_wird_nicht_escaped(self, db, person):
        """Regression: der Empfaenger sah den rohen HTML-Code als Text.

        Ursache war ``markupsafe.escape``: es liefert ein ``Markup``, und bei
        ``"literal" + Markup(...)`` bevorzugt Python ``Markup.__radd__``, das
        auch das Literal escaped. Dadurch wurde ``<p>`` zu ``&lt;p&gt;``.
        """
        dep, _entry = acc.post_deposit(db, person, 500, send_email=False)
        db.commit()

        _subject, text, html = dep_svc.build_deposit_message(
            dep, person, app_name="Snack & Bar", balance_cents=dep.balance_after_cents
        )

        assert "&lt;" not in html, "HTML-Markup wurde escaped"
        assert "<p>Hallo" in html
        assert "<table>" in html
        assert "</html>" in html
        # Der Text-Teil bleibt echter Text.
        assert "<p>" not in text

    def test_html_inhalt_wird_still_escaped(self, db, person):
        """Werte im HTML muessen escaped bleiben - nur die Tags nicht."""
        person.first_name = "<b>Max</b>"
        db.commit()
        dep, _entry = acc.post_deposit(db, person, 500, send_email=False)
        db.commit()

        _subject, _text, html = dep_svc.build_deposit_message(
            dep, person, app_name="Bar", balance_cents=dep.balance_after_cents
        )

        assert "<b>Max</b>" not in html, "Name wurde nicht escaped"
        assert "&lt;b&gt;Max&lt;/b&gt;" in html
        assert "&lt;p&gt;" not in html, "die Tags selbst sind escaped"

    def test_fusszeile_zeigt_keine_leere_rechnungsnummer(self, db, person):
        """Die Einzahlung hat keine Rechnungsnummer - die Fusszeile auch nicht."""
        dep, _entry = acc.post_deposit(db, person, 500, send_email=False)
        db.commit()

        _subject, _text, html = dep_svc.build_deposit_message(
            dep, person, app_name="Bar", balance_cents=dep.balance_after_cents
        )

        assert "Rechnungsnummer" not in html
        assert "Eingang am" in html

    def test_notiz_erscheint_in_beiden_teilen(self, db, person):
        dep, _entry = acc.post_deposit(db, person, 500, note="Bar im Tresen", send_email=False)
        db.commit()

        _subject, text, html = dep_svc.build_deposit_message(
            dep, person, app_name="Bar", balance_cents=dep.balance_after_cents
        )

        assert "Bar im Tresen" in text
        assert "Bar im Tresen" in html

    def test_retry_vieler_einzahlungen_aendert_keine_saldo(self, db, person, mailer):
        # Ohne E-Mail-Wunsch erfasst, damit sie in der Warteschlange landen.
        for cents in (300, 400):
            acc.post_deposit(db, person, cents, send_email=True)
        db.commit()
        before = _balance(db, person.id)

        stats = dep_svc.retry_pending_deposit_emails(db, Settings(db), mailer=mailer)
        db.commit()

        assert stats["considered"] == 2
        assert stats["sent"] == 2
        assert _balance(db, person.id) == before == 700

    def test_ohne_email_wunsch_bleibt_status_nicht_gesendet(self, db, person):
        dep, _entry = acc.post_deposit(db, person, 100, send_email=False)
        db.commit()

        assert dep.email_status == DepositEmailStatus.NICHT_GESENDET
        # Eine ohne Mail erfasste Einzahlung wird auch nicht nachversendet.
        stats = dep_svc.retry_pending_deposit_emails(
            db, Settings(db), mailer=_ok_mailer()
        )
        db.commit()
        assert stats["considered"] == 0

    def test_mailversuche_wird_gezaehlt_und_status_laeuft_durch(
        self, db, person, mailer
    ):
        dep, _entry = acc.post_deposit(db, person, 900, send_email=True)
        db.commit()
        assert dep.email_status == DepositEmailStatus.OFFEN
        assert dep.email_attempts == 0

        dep_svc.send_deposit_confirmation(
            db, Settings(db), dep, mailer=_broken_mailer(), retry_delay=0.0
        )
        db.commit()
        assert dep.email_attempts == 1
        assert dep.email_status == DepositEmailStatus.FEHLGESCHLAGEN

        dep_svc.send_deposit_confirmation(db, Settings(db), dep, mailer=mailer)
        db.commit()
        assert dep.email_attempts == 2
        assert dep.email_status == DepositEmailStatus.GESENDET
        assert dep.email_error is None
        # Unabhaengig von drei Mailversuchen ist das Geld genau einmal gebucht.
        assert _balance(db, person.id) == 900
        assert db.query(LedgerEntry).filter_by(deposit_id=dep.id).count() == 1


# ---------------------------------------------------------------------------
# Aufräumen und Konten
# ---------------------------------------------------------------------------
class TestBackupUndRestore:
    def test_backup_enthaelt_die_neuen_tabellen(self, db, person):
        from app.services import backup as bkp

        dep_svc_post(db, person, 1000)
        row = _book(db, person, 300)
        acc.post_consumption(db, row)
        db.commit()

        data = bkp.build_backup(db)
        entities = data.get("data", data)

        assert entities.get("accounts"), "accounts fehlt im Backup"
        assert entities.get("deposits"), "deposits fehlt im Backup"
        assert entities.get("ledger_entries"), "ledger_entries fehlt im Backup"

    def test_restore_erhaelt_das_konto_und_den_saldo(self, db, person, app):
        """Ein Backup-Restore muss Kontostand und Historie wiederherstellen."""
        from sqlalchemy import create_engine, text

        from app.models import Base
        from app.services import backup as bkp

        dep_svc_post(db, person, 2500)
        row = _book(db, person, 700)
        acc.post_consumption(db, row)
        db.commit()

        expected_balance = _balance(db, person.id)
        expected_entries = len(_ledger(db, person.id))
        payload = bkp.build_backup(db)
        db.commit()

        # In eine frische Datenbank zurueckspielen.
        fresh_path = pathlib.Path(db.get_bind().url.database).parent / "restore.db"
        fresh_engine = create_engine(f"sqlite:///{fresh_path.as_posix()}")
        Base.metadata.create_all(fresh_engine)
        FreshSession = sessionmaker(bind=fresh_engine)

        with FreshSession() as fresh:
            bkp.restore_from_json(fresh, payload, mode="replace")
            fresh.commit()
            restored = fresh.execute(
                select(func.coalesce(func.sum(LedgerEntry.amount_cents), 0))
            ).scalar_one()
            assert int(restored) == expected_balance
            assert fresh.query(LedgerEntry).count() == expected_entries
            assert fresh.query(Deposit).count() == 1
            assert fresh.query(Account).count() == 1
        fresh_engine.dispose()


class TestKonten:
    def test_jede_person_bekommt_genau_ein_konto(self, db, configured):
        from app.models import Product

        extra = Person(first_name="Zwei", last_name="Test", email="zwei@example.com",
                       is_active=True)
        db.add(extra)
        db.commit()

        for person_id in (configured["person_id"], extra.id):
            acc.get_or_create_account(db, db.get(Person, person_id))
            acc.get_or_create_account(db, db.get(Person, person_id))
        db.commit()

        counts = db.query(Account).all()
        assert len({a.person_id for a in counts}) == len(counts)

    def test_kontostand_ist_immer_die_summe_des_ledgers(self, db, person):
        dep_svc_post(db, person, 1000)
        for cents in (200, 300, 150):
            r = _book(db, person, cents)
            acc.post_consumption(db, r)
        db.commit()
        acc.adjust_balance(db, person, -50, reason="Korrektur")
        db.commit()

        assert acc.verify_accounts(db) == []

    def test_neue_kontobewegung_ohne_person_kommt_nicht_hin(self, db, person):
        row = _book(db, person, 100)
        acc.post_consumption(db, row)
        db.commit()
        entries = _ledger(db, person.id)

        assert len(entries) == 1
        assert entries[0].balance_after_cents == _balance(db, person.id)

    def test_keine_geldwerte_sind_fliesskomma(self, db, person):
        dep = dep_svc_post(db, person, 999)
        entry = db.query(LedgerEntry).filter_by(deposit_id=dep.id).one()
        for value in (dep.amount_cents, entry.amount_cents,
                      entry.balance_before_cents, entry.balance_after_cents,
                      _balance(db, person.id)):
            assert isinstance(value, int)
            assert not isinstance(value, float)


# ---------------------------------------------------------------------------
# Rueckuebernahme von Bestandsdaten
# ---------------------------------------------------------------------------
class TestRueckuebernahmeMitEinzahlungen:
    def test_rueckuebernahme_rechnet_einzahlungen_ein(self, db, person):
        """Einzahlungen gehoeren in die Rueckuebernahme.

        Regression: die Rueckuebernahme spulte nur Buchungen nach, wodurch
        vorhandenes Guthaben fehlte und der Kontostand zu niedrig wurde.
        """
        dep_svc_post(db, person, 10000)
        rows = []
        for cents in (2500, 1500):
            r = _book(db, person, cents)
            rows.append(r)
        db.commit()

        _clear_account_data(db)
        acc.ensure_accounts(db, force=True)
        db.commit()

        # Guthaben zuerst, dann die Buchungen - beide in zeitlicher Folge.
        assert _balance(db, person.id) == 10000 - 2500 - 1500
        assert [r.credit_applied_cents for r in rows] == [2500, 1500]
        assert [r.balance_before_cents for r in rows] == [10000, 7500]
        types = [e.entry_type.value for e in _ledger(db, person.id)]
        assert types == ["EINZAHLUNG", "VERZEHR", "VERZEHR"]

    def test_rueckuebernahme_bleibt_idempotent(self, db, person):
        dep_svc_post(db, person, 5000)
        r = _book(db, person, 1000)
        acc.post_consumption(db, r)
        db.commit()
        balance_vorher = _balance(db, person.id)
        ledger_vorher = len(_ledger(db, person.id))

        for _ in range(3):
            acc.ensure_accounts(db, force=True)
            db.commit()

        assert _balance(db, person.id) == balance_vorher
        assert len(_ledger(db, person.id)) == ledger_vorher
        # Keine doppelte Einzahlung trotz UNIQUE auf deposit_id.
        assert db.query(LedgerEntry).filter_by(deposit_id=db.query(Deposit).one().id).count() == 1


class TestRueckuebernahme:
    def test_bestehende_buchungen_werden_uebernommen(self, db, configured, person):
        from app.models import Consumption as C

        rows = []
        for cents in (200, 300):
            r = C(person_id=person.id, product_name="Alt", unit_price_cents=cents,
                  quantity=1, total_cents=cents, currency="EUR",
                  status=ConsumptionStatus.OFFEN)
            db.add(r)
            rows.append(r)
        db.commit()

        stats = acc.ensure_accounts(db, force=True)
        db.commit()

        assert stats["entries_created"] == 2
        assert _balance(db, person.id) == -500
        assert acc.verify_accounts(db) == []

    def test_stornierte_altbuchung_belegt_nicht(self, db, person):
        from app.models import Consumption as C

        r = C(person_id=person.id, product_name="Alt", unit_price_cents=500,
              quantity=1, total_cents=500, currency="EUR",
              status=ConsumptionStatus.STORNIERT)
        db.add(r)
        db.commit()

        acc.ensure_accounts(db, force=True)
        db.commit()

        assert _balance(db, person.id) == 0
        assert _ledger(db, person.id) == []

    def test_rueckuebernahme_erfindet_keine_einzahlungen(self, db, person):
        row = _book(db, person, 400)
        acc.post_consumption(db, row)
        db.commit()

        acc.ensure_accounts(db, force=True)
        db.commit()

        assert _balance(db, person.id) == -400
        assert db.query(Deposit).count() == 0

    def test_rueckuebernahme_ist_wiederholbar(self, db, person):
        for cents in (200, 300):
            r = _book(db, person, cents)
            acc.post_consumption(db, r)
        db.commit()
        before = _balance(db, person.id)
        count = len(_ledger(db, person.id))

        acc.ensure_accounts(db, force=True)
        acc.ensure_accounts(db, force=True)
        db.commit()

        assert _balance(db, person.id) == before
        assert len(_ledger(db, person.id)) == count

    def test_zweiter_start_laeuft_als_noop(self, db, person):
        row = _book(db, person, 300)
        acc.post_consumption(db, row)
        db.commit()

        first = acc.ensure_accounts(db, force=True)
        db.commit()
        assert first["already_done"] is False
        count = len(_ledger(db, person.id))
        balance = _balance(db, person.id)

        second = acc.ensure_accounts(db)
        db.commit()

        assert second["already_done"] is True
        assert second["entries_created"] == 0
        assert len(_ledger(db, person.id)) == count
        assert _balance(db, person.id) == balance

    def test_altkonto_mit_historie_wird_nicht_ueberschrieben(self, db, person):
        dep_svc_post(db, person, 1000)
        row = _book(db, person, 200)
        acc.post_consumption(db, row)
        db.commit()
        assert _balance(db, person.id) == 800

        stats = acc.ensure_accounts(db, force=True)
        db.commit()

        assert stats["skipped_existing"] >= 1
        assert stats["entries_created"] == 0
        assert _balance(db, person.id) == 800

    def test_altperson_bekommt_konto_beim_start(self, db):
        neu = Person(first_name="Alt", last_name="Person", email="altperson@example.com",
                     is_active=True)
        db.add(neu)
        db.commit()

        acc.ensure_accounts(db, force=True)
        db.commit()

        assert acc.get_account(db, neu.id) is not None
        assert _balance(db, neu.id) == 0