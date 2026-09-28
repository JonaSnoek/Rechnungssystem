"""Die Abrechnungsregeln aus der Spezifikation (Abschnitt 1-15).

Jeder Test entspricht einem nummerierten Abschnitt der Anforderung.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.models import ConsumptionStatus, InvoiceStatus, PaymentStatus
from conftest import BASE_DATE

pytestmark = pytest.mark.usefixtures("configured")


# ---------------------------------------------------------------------------
# 1) Grundprinzip
# ---------------------------------------------------------------------------
class TestGrundprinzip:
    def test_offener_betrag_wird_abgerechnet(self, book, bill, balance, invoices,
                                              configured):
        pid = configured["person_id"]
        book(pid, "Spezi", 200, 1)
        book(pid, "Kinder Country", 80, 2)
        assert balance() == 360

        result = bill()

        assert result.emails_sent == 1
        assert result.total_cents == 360
        assert balance() == 0
        assert invoices()[0].status == InvoiceStatus.VERSENDT

    def test_eine_e_mail_pro_rechnung(self, mailer, book, bill, balance, configured):
        book(configured["person_id"], "Spezi", 200)
        bill()
        assert len(mailer.sent) == 1

    def test_null_betrag_wird_nicht_versendet(self, book, bill, mailer, configured):
        book(configured["person_id"], "Kostenlos", 0, 1)
        result = bill()
        assert result.emails_sent == 0
        assert mailer.sent == []


# ---------------------------------------------------------------------------
# 2) + 3) Neue Buchungen nach dem Versand
# ---------------------------------------------------------------------------
class TestNachVersand:
    def test_neue_buchungen_gehoeren_zur_naechsten_rechnung(
        self, book, bill, balance, invoices, configured
    ):
        pid = configured["person_id"]
        book(pid, "Spezi", 200)
        book(pid, "Kinder Country", 80, 2)
        bill()
        first = invoices()[0]

        book(pid, "Spezi", 200)
        book(pid, "Wasser", 100)
        assert balance() == 300
        assert first.total_cents == 360  # alte Rechnung bleibt unveraendert

        bill()

        second = invoices()[1]
        assert second.total_cents == 300
        assert sorted((i.product_name, i.quantity) for i in second.items) == [
            ("Spezi", 1),
            ("Wasser", 1),
        ]
        assert balance() == 0

    def test_keine_buchung_ist_zwei_rechnungen_zugeordnet(
        self, book, bill, db, invoices
    ):
        book(1, "Spezi", 200)
        bill()
        book(1, "Wasser", 100)
        bill()
        db.expire_all()
        for invoice in invoices():
            if invoice.status == InvoiceStatus.VERSENDT:
                for c in invoice.consumptions:
                    assert c.invoice_id == invoice.id


# ---------------------------------------------------------------------------
# 4) + 5) Manuelle Rechnung
# ---------------------------------------------------------------------------
class TestManuelleRechnung:
    def test_manuelle_rechnung_billt_alles_offene(self, book, bill, balance,
                                                   configured):
        pid = configured["person_id"]
        for _ in range(5):
            book(pid, "Spezi", 170)
        assert balance() == 850

        result = bill(trigger="manual")

        assert result.total_cents == 850
        assert balance() == 0

    def test_manuelle_rechnung_ist_kein_zeitpunkt(self, book, bill, balance,
                                                    invoices, configured):
        pid = configured["person_id"]
        book(pid, "Spezi", 200)
        bill()                      # 17:00
        book(pid, "Spezi", 200)     # 17:05
        assert balance() == 200
        bill(trigger="manual")      # 17:30
        assert balance() == 0
        book(pid, "Kaffee", 150)    # 18:00
        assert balance() == 150
        assert len(invoices()) == 2
        assert [i.sequence for i in invoices()] == [1, 2]


# ---------------------------------------------------------------------------
# 9) Mehrere Rechnungen pro Tag
# ---------------------------------------------------------------------------
class TestMehrereRechnungenProTag:
    def test_drei_rechnungen_an_einem_tag(self, book, bill, invoices, configured):
        pid = configured["person_id"]
        for amount in (590, 300, 150):
            book(pid, "Spezi", amount)
            bill()
        rows = invoices()
        assert len(rows) == 3
        assert [r.sequence for r in rows] == [1, 2, 3]
        assert [r.total_cents for r in rows] == [590, 300, 150]

    def test_rechnungsnummern_sind_eindeutig(self, book, bill, invoices, configured):
        pid = configured["person_id"]
        for _ in range(4):
            book(pid, "Spezi", 100)
            bill()
        numbers = [r.invoice_number for r in invoices()]
        assert len(set(numbers)) == len(numbers) == 4

    def test_sequence_wird_nie_wiederverwendet(self, book, bill, invoices, db):
        from app.services.billing import delete_invoice

        book(1, "Spezi", 100)
        bill()
        book(1, "Spezi", 100)
        bill()
        delete_invoice(db, invoices()[0])
        book(1, "Kaffee", 150)
        bill()
        rows = invoices()
        # the deleted slot is not handed out again - sequences only grow
        assert [r.sequence for r in rows] == [2, 3]
        assert len({r.invoice_number for r in rows}) == 2


# ---------------------------------------------------------------------------
# 6) + 11) + 15) Zuordnung statt Loeschung
# ---------------------------------------------------------------------------
class TestZuordnung:
    def test_buchungen_werden_nie_geloescht(self, book, bill, db, configured):
        book(configured["person_id"], "Spezi", 200)
        bill()
        assert db.query(__import__("app.models", fromlist=["Consumption"]).Consumption).count() == 1

    def test_offener_betrag_ist_die_summe_nicht_zugeordneter_buchungen(
        self, book, bill, balance, db, configured
    ):
        from app.models import Consumption

        pid = configured["person_id"]
        book(pid, "Kaffee", 150)
        book(pid, "Kaffee", 150)
        assert balance() == 300
        bill()
        assert balance() == 0
        open_rows = db.query(Consumption).filter(
            Consumption.status == ConsumptionStatus.OFFEN
        ).count()
        assert open_rows == 0

    def test_gruppen_nach_produkt_und_preis(self, book, bill, invoices, configured):
        pid = configured["person_id"]
        book(pid, "Spezi", 200)
        book(pid, "Spezi", 200)
        book(pid, "Kaffee", 150)
        bill()
        items = {i.product_name: (i.quantity, i.total_cents) for i in invoices()[0].items}
        assert items == {"Spezi": (2, 400), "Kaffee": (1, 150)}

    def test_snapshot_preis_bleibt_erhalten(self, book, bill, db, configured):
        from app.models import Consumption, Product

        book(configured["person_id"], "Spezi", 200, 1)
        bill()
        product = db.query(Product).filter_by(name="Spezi").one()
        product.price_cents = 999
        db.commit()
        row = db.query(Consumption).one()
        assert row.unit_price_cents == 200
        assert row.product_name == "Spezi"
        assert row.invoice_id is not None


# ---------------------------------------------------------------------------
# 7) Versandfehler
# ---------------------------------------------------------------------------
class TestVersandfehler:
    def test_betrag_bleibt_offen(self, book, bill, balance, invoices, mailer,
                                 configured):
        from app.mailer import SendResult

        pid = configured["person_id"]
        book(pid, "Spezi", 500)
        before = balance()

        broken = type("Broken", (), {
            "fail": True,
            "send": lambda self, to, subject, text_body, html_body: SendResult(
                ok=False, error="SMTP nicht erreichbar"
            ),
        })()
        result = bill(use_mailer=broken)

        assert result.emails_sent == 0
        assert result.emails_failed == 1
        assert balance() == before
        failed = [i for i in invoices() if i.status == InvoiceStatus.FEHLGESCHLAGEN]
        assert len(failed) == 1
        assert failed[0].total_cents == 500
        assert failed[0].payment_status == PaymentStatus.OFFEN

    def test_buchungen_bleiben_offen_und_zugeordnet(self, book, bill, db, invoices,
                                                    mailer, configured):
        from app.mailer import SendResult
        from app.models import Consumption

        pid = configured["person_id"]
        book(pid, "Spezi", 500)
        book(pid, "Kaffee", 150)
        broken = type("Broken", (), {
            "send": lambda self, to, subject, text_body, html_body: SendResult(
                ok=False, error="down"
            ),
        })()
        bill(use_mailer=broken)

        failed = [i for i in invoices() if i.status == InvoiceStatus.FEHLGESCHLAGEN][0]
        linked = db.query(Consumption).filter_by(invoice_id=failed.id).all()
        assert len(linked) == 2
        assert all(c.status == ConsumptionStatus.OFFEN for c in linked)
        assert all(c.invoice_id == failed.id for c in linked)

    def test_erneuter_versand_schliesst_die_buchungen(self, book, bill, balance,
                                                       db, invoices, mailer,
                                                       configured):
        from app.mailer import SendResult
        from app.models import Consumption

        pid = configured["person_id"]
        book(pid, "Spezi", 500)
        broken = type("Broken", (), {
            "send": lambda self, to, subject, text_body, html_body: SendResult(
                ok=False, error="down"
            ),
        })()
        bill(use_mailer=broken)
        failed = [i for i in invoices() if i.status == InvoiceStatus.FEHLGESCHLAGEN][0]

        result = bill()

        assert result.emails_sent == 1
        assert balance() == 0
        db.refresh(failed)
        assert failed.status == InvoiceStatus.VERSENDT
        linked = db.query(Consumption).filter_by(invoice_id=failed.id).all()
        assert all(c.status == ConsumptionStatus.ABGERECHNET for c in linked)
        assert len(invoices()) == 1  # keine Doppelabrechnung

    def test_fehlerhafte_rechnung_wird_nicht_doppelt_erstellt(
        self, book, bill, invoices, mailer, configured
    ):
        from app.mailer import SendResult

        pid = configured["person_id"]
        book(pid, "Spezi", 500)
        broken = type("Broken", (), {
            "send": lambda self, to, subject, text_body, html_body: SendResult(
                ok=False, error="down"
            ),
        })()
        bill(use_mailer=broken)
        bill(use_mailer=broken)   # zweiter Versuch scheitert erneut
        assert len(invoices()) == 1

    def test_storno_gibt_buchungen_frei(self, book, bill, balance, db, invoices,
                                         configured):
        from app.services.billing import cancel_invoice

        book(configured["person_id"], "Spezi", 200)
        bill()
        assert balance() == 0

        cancel_invoice(db, invoices()[0])

        assert invoices()[0].status == InvoiceStatus.STORNIERT
        assert balance() == 200
        bill()
        assert balance() == 0


# ---------------------------------------------------------------------------
# 12) Die Uhrzeit ist nur ein Ausloeser
# ---------------------------------------------------------------------------
class TestZeitpunktNurTrigger:
    def test_zeitstempel_der_rechnung(self, book, bill, invoices, configured):
        book(configured["person_id"], "Spezi", 200)
        bill()
        invoice = invoices()[0]
        assert invoice.created_at is not None
        assert invoice.sent_at is not None

    def test_kein_taeglicher_zustand(self, book, bill, balance, configured):
        pid = configured["person_id"]
        for amount in (200, 200, 150, 150):
            book(pid, "Spezi", amount)
            bill()
            assert balance() == 0


# ---------------------------------------------------------------------------
# 8) Rechnung als eigener Datensatz
# ---------------------------------------------------------------------------
class TestRechnungsdatensatz:
    def test_felder_einer_rechnung(self, book, bill, invoices, configured):
        book(configured["person_id"], "Kaffee", 150)
        bill()
        inv = invoices()[0]
        assert inv.invoice_number.startswith("RE-")
        assert inv.person_id == configured["person_id"]
        assert inv.total_cents == 150
        assert inv.currency == "EUR"
        assert inv.status == InvoiceStatus.VERSENDT
        assert inv.paypal_link == "https://www.paypal.me/JONASNOEK1/1.50EUR"
        assert inv.paypal_username == "JONASNOEK1"
        assert inv.email_subject
        assert inv.email_body
        assert inv.email_html
        assert len(inv.items) == 1

    def test_positionen_sind_aggregiert(self, book, bill, invoices, configured):
        pid = configured["person_id"]
        book(pid, "Kinder Country", 80, 3)
        bill()
        item = invoices()[0].items[0]
        assert (item.product_name, item.quantity, item.total_cents) == (
            "Kinder Country", 3, 240
        )


# ---------------------------------------------------------------------------
# 14) Historie
# ---------------------------------------------------------------------------
class TestHistorie:
    def test_alle_rechnungen_bleiben_erhalten(self, book, bill, db, invoices,
                                              configured):
        pid = configured["person_id"]
        for amount in (100, 200, 300):
            book(pid, "Spezi", amount)
            bill()
        rows = invoices()
        assert [r.total_cents for r in rows] == [100, 200, 300]
        assert all(r.items for r in rows)

    def test_summe_der_rechnungen_entspricht_der_summe_der_buchungen(
        self, book, bill, db, invoices, configured
    ):
        from app.models import Consumption

        pid = configured["person_id"]
        for amount in (150, 250, 350):
            book(pid, "Spezi", amount)
            bill()
        book(pid, "Kaffee", 150)
        bill()
        billed = sum(i.total_cents for i in invoices()
                     if i.status == InvoiceStatus.VERSENDT)
        booked = sum(
            c.total_cents for c in db.query(Consumption).all()
            if c.status == ConsumptionStatus.ABGERECHNET
        )
        assert billed == booked


# ---------------------------------------------------------------------------
# Retry ueber Tagesgrenzen hinweg
# ---------------------------------------------------------------------------
class TestRetryUeberTage:
    def test_fehlgeschlagene_rechnung_vom_vortag_wird_wieder_versendet(
        self, book, bill, balance, invoices, mailer, configured
    ):
        pid = configured["person_id"]
        book(pid, "Spezi", 200)
        mailer.fail = True
        bill()                                   # yesterday: delivery fails
        assert invoices()[0].status == InvoiceStatus.FEHLGESCHLAGEN
        assert balance() == 200

        mailer.fail = False
        book(pid, "Kaffee", 150)                  # a new booking today
        result = bill(period=BASE_DATE + timedelta(days=1))

        assert [i.status for i in invoices()] == [
            InvoiceStatus.VERSENDT, InvoiceStatus.VERSENDT
        ]
        assert result.emails_failed == 0
        assert balance() == 0

    def test_fehlgeschlagene_rechnung_wird_erst_wiederholt_dann_neue_erstellt(
        self, book, bill, balance, invoices, mailer, configured
    ):
        pid = configured["person_id"]
        book(pid, "Spezi", 200)
        mailer.fail = True
        bill()
        mailer.fail = False
        book(pid, "Kaffee", 150)
        bill(period=BASE_DATE + timedelta(days=1))

        rows = invoices()
        assert len(rows) == 2
        # the retry kept exactly the original amount, the new invoice only 150
        assert [r.total_cents for r in rows] == [200, 150]
        assert rows[0].period_date == BASE_DATE
        assert rows[1].period_date == BASE_DATE + timedelta(days=1)

    def test_erfolgreich_versandte_rechnung_wird_nicht_wiederholt(
        self, book, bill, mailer, invoices, configured
    ):
        pid = configured["person_id"]
        book(pid, "Spezi", 200)
        bill()
        before = len(mailer.sent)
        book(pid, "Kaffee", 150)
        bill(period=BASE_DATE + timedelta(days=1))
        # one retry-free invoice: only the new booking is mailed
        assert len(mailer.sent) == before + 1
        assert invoices()[0].status == InvoiceStatus.VERSENDT
        assert invoices()[1].total_cents == 150

    def test_mehrere_offene_rechnungen_einer_person_werden_alle_wiederholt(
        self, book, bill, mailer, invoices, balance, db, configured
    ):
        """Repairs legacy data: one person, several undelivered invoices."""
        from app.models import Consumption

        pid = configured["person_id"]
        for day, amount in enumerate((100, 200, 300)):
            book(pid, "Spezi", amount, at=BASE_DATE - timedelta(days=3 - day))
            bill(period=BASE_DATE - timedelta(days=3 - day))
        assert len(invoices()) == 3
        assert all(i.status == InvoiceStatus.VERSENDT for i in invoices())

        # simulate an outage that hit three invoices of the same person
        for row in invoices():
            row.status = InvoiceStatus.FEHLGESCHLAGEN
        for row in db.query(Consumption).all():
            row.status = ConsumptionStatus.OFFEN
        db.commit()
        assert balance() == 600

        result = bill(period=BASE_DATE)

        assert result.emails_sent == 3
        assert all(i.status == InvoiceStatus.VERSENDT for i in invoices())
        assert balance() == 0

    def test_neue_rechnungen_entstehen_nicht_waehrend_eines_ausfalls(
        self, book, bill, mailer, invoices, balance, configured
    ):
        """A broken SMTP must not pile up a new invoice per run."""
        pid = configured["person_id"]
        mailer.fail = True
        for day, amount in enumerate((100, 200)):
            book(pid, "Spezi", amount)
            bill()
        assert len(invoices()) == 1
        assert balance() == 300
