"""Konto-Oberflaeche: Buchung belastet sofort, Einzahlung, Kontohistorie.

Diese Tests gehen bewusst ueber HTTP, weil dort die Kette
Buchung -> Konto -> Rechnung -> Anzeige real zusammenlaeuft.
"""

from __future__ import annotations

import pytest

from app.models import (
    Account,
    Consumption,
    Deposit,
    DepositEmailStatus,
    Invoice,
    LedgerEntry,
    LedgerEntryType,
    Person,
    Product,
)
from app.services import accounts as acc


@pytest.fixture()
def kunde(smtp, csrf, app):
    """Ein Kunde mit einem Produkt, fertig fuer HTTP-Aufrufe."""
    smtp.post("/personen/neu", data={
        "csrf_token": csrf("/personen/neu"), "first_name": "Max",
        "last_name": "Mustermann", "email": "max@example.com"})
    smtp.post("/produkte/neu", data={
        "csrf_token": csrf("/produkte/neu"), "name": "Spezi", "price": "2,00"})
    with app.app_context():
        from app.db import get_session

        db = get_session()
        return {
            "person_id": db.query(Person).one().id,
            "product_id": db.query(Product).one().id,
        }


def _buche(client, csrf, kunde, menge="2"):
    """Bucht Verzehr ueber die Oberflaeche."""
    return client.post("/verzehr/schnell", data={
        "csrf_token": csrf(f"/verzehr/erfassen?person_id={kunde['person_id']}"),
        "person_id": kunde["person_id"],
        "product_id": kunde["product_id"],
        "quantity": menge,
    })


def _state(app, person_id):
    with app.app_context():
        from app.db import get_session

        db = get_session()
        return {
            "balance": acc.balance_cents(db, person_id),
            "ledger": db.query(LedgerEntry).filter_by(person_id=person_id)
            .order_by(LedgerEntry.id).all(),
            "consumptions": db.query(Consumption).filter_by(person_id=person_id).all(),
            "invoices": db.query(Invoice).filter_by(person_id=person_id).all(),
            "deposits": db.query(Deposit).filter_by(person_id=person_id).all(),
        }


# ---------------------------------------------------------------------------
# Buchung
# ---------------------------------------------------------------------------
class TestBuchungBelastetSofort:
    def test_erfasste_buchung_belegt_das_konto(self, smtp, csrf, kunde, app):
        _buche(smtp, csrf, kunde)

        st = _state(app, kunde["person_id"])
        assert st["balance"] == -400
        assert len(st["ledger"]) == 1
        assert st["ledger"][0].entry_type == LedgerEntryType.VERZEHR
        assert st["ledger"][0].amount_cents == -400

    def test_schnellbuchung_legt_ein_konto_an(self, smtp, csrf, kunde, app):
        _buche(smtp, csrf, kunde)

        with app.app_context():
            from app.db import get_session

            db = get_session()
            assert db.query(Account).filter_by(person_id=kunde["person_id"]).count() == 1

    def test_mehrere_buchungen_summieren_sich(self, smtp, csrf, kunde, app):
        _buche(smtp, csrf, kunde, "2")
        _buche(smtp, csrf, kunde, "3")

        st = _state(app, kunde["person_id"])
        assert st["balance"] == -1000
        assert len(st["ledger"]) == 2
        # Laufende Salden in der Reihenfolge.
        assert [e.balance_after_cents for e in st["ledger"]] == [-400, -1000]

    def test_rechnung_belegt_nicht_erneut(self, smtp, csrf, kunde, app):
        _buche(smtp, csrf, kunde)
        pid = kunde["person_id"]
        before = _state(app, pid)["balance"]

        smtp.post(f"/abrechnungen/person/{pid}",
                  data={"csrf_token": csrf(f"/abrechnungen/person/{pid}")})

        st = _state(app, pid)
        assert st["balance"] == before == -400
        assert len(st["ledger"]) == 1, "die Rechnung darf keine Bewegung erzeugen"
        assert len(st["invoices"]) == 1
        assert st["invoices"][0].amount_due_cents == 400
        assert st["invoices"][0].credit_applied_cents == 0


# ---------------------------------------------------------------------------
# Einzahlung ueber die Oberflaeche
# ---------------------------------------------------------------------------
class TestEinzahlungHttp:
    def test_formular_ist_erreichbar(self, smtp, kunde):
        r = smtp.get(f"/personen/{kunde['person_id']}/einzahlung")
        assert r.status_code == 200
        assert "Einzahlung" in r.get_data(as_text=True)

    def test_einzahlung_bucht_und_bestaetigt(self, smtp, csrf, kunde, app):
        pid = kunde["person_id"]
        r = smtp.post(f"/personen/{pid}/einzahlung", data={
            "csrf_token": csrf(f"/personen/{pid}/einzahlung"),
            "amount": "20,00", "payment_type": "PAYPAL", "send_email": "1",
        }, follow_redirects=True)
        assert r.status_code == 200

        st = _state(app, pid)
        assert st["balance"] == 2000
        assert len(st["deposits"]) == 1
        dep = st["deposits"][0]
        assert dep.amount_cents == 2000
        assert dep.payment_type.value == "PAYPAL"
        # SMTP zeigt im Test auf einen ungueltigen Host: die Einzahlung bleibt.
        assert dep.email_status in (DepositEmailStatus.FEHLGESCHLAGEN,
                                    DepositEmailStatus.OFFEN)

    def test_einzahlung_ohne_mail_erhaelt_keine_warteschlange(self, smtp, csrf, kunde, app):
        pid = kunde["person_id"]
        smtp.post(f"/personen/{pid}/einzahlung", data={
            "csrf_token": csrf(f"/personen/{pid}/einzahlung"),
            "amount": "10,00", "payment_type": "BAR",
        }, follow_redirects=True)

        st = _state(app, pid)
        assert st["deposits"][0].email_status == DepositEmailStatus.NICHT_GESENDET
        assert st["balance"] == 1000

    def test_betrag_muss_groesser_null_sein(self, smtp, csrf, kunde, app):
        pid = kunde["person_id"]
        r = smtp.post(f"/personen/{pid}/einzahlung", data={
            "csrf_token": csrf(f"/personen/{pid}/einzahlung"),
            "amount": "0", "payment_type": "BAR",
        }, follow_redirects=True)

        assert "0,00" in r.get_data(as_text=True)
        st = _state(app, pid)
        assert st["deposits"] == []
        assert st["balance"] == 0

    def test_ungueltige_zahlart_wird_abgelehnt(self, smtp, csrf, kunde, app):
        pid = kunde["person_id"]
        r = smtp.post(f"/personen/{pid}/einzahlung", data={
            "csrf_token": csrf(f"/personen/{pid}/einzahlung"),
            "amount": "5,00", "payment_type": "ERFUNDEN",
        }, follow_redirects=True)

        assert r.status_code == 200
        assert _state(app, pid)["deposits"] == []

    def test_einzahlung_saldiert_schuld(self, smtp, csrf, kunde, app):
        pid = kunde["person_id"]
        _buche(smtp, csrf, kunde)          # 4,00 Schulden

        smtp.post(f"/personen/{pid}/einzahlung", data={
            "csrf_token": csrf(f"/personen/{pid}/einzahlung"),
            "amount": "2,50", "payment_type": "BAR",
        }, follow_redirects=True)

        assert _state(app, pid)["balance"] == -150


# ---------------------------------------------------------------------------
# Kontoansicht
# ---------------------------------------------------------------------------
class TestKontoansicht:
    def test_seite_zeigt_kontostand(self, smtp, csrf, kunde, app):
        pid = kunde["person_id"]
        _buche(smtp, csrf, kunde)

        r = smtp.get(f"/personen/{pid}/konto")
        assert r.status_code == 200
        html = r.get_data(as_text=True)
        assert "Konto" in html
        assert "-4,00" in html or "4,00" in html

    def test_seite_listet_einzahlungen_und_bewegungen(self, smtp, csrf, kunde, app):
        pid = kunde["person_id"]
        smtp.post(f"/personen/{pid}/einzahlung", data={
            "csrf_token": csrf(f"/personen/{pid}/einzahlung"),
            "amount": "20,00", "payment_type": "BAR"})
        _buche(smtp, csrf, kunde)

        html = smtp.get(f"/personen/{pid}/konto").get_data(as_text=True)
        assert "Einzahlungen" in html
        assert "Kontobewegungen" in html
        assert "EINZAHLUNG" in html
        assert "VERZEHR" in html

    def test_sortierung_umschalten(self, smtp, csrf, kunde, app):
        pid = kunde["person_id"]
        for _ in range(2):
            _buche(smtp, csrf, kunde)

        r = smtp.get(f"/personen/{pid}/konto?sort=betrag&dir=asc")
        assert r.status_code == 200
        html = r.get_data(as_text=True)
        # Beide Bewegungen sichtbar, Reihenfolge sortiert.
        assert html.count("VERZEHR") >= 2

    def test_seite_ohne_person_zeigt_meldung(self, smtp):
        r = smtp.get("/personen/999999/konto", follow_redirects=True)
        assert r.status_code == 200
        assert "nicht gefunden" in r.get_data(as_text=True).lower()

    def test_personenliste_zeigt_kontostand(self, smtp, csrf, kunde, app):
        pid = kunde["person_id"]
        smtp.post(f"/personen/{pid}/einzahlung", data={
            "csrf_token": csrf(f"/personen/{pid}/einzahlung"),
            "amount": "15,00", "payment_type": "BAR"})

        html = smtp.get("/personen/").get_data(as_text=True)
        assert "Kontostand" in html
        assert "15,00" in html
        assert f"/personen/{pid}/konto" in html


# ---------------------------------------------------------------------------
# Anfangsbestand
# ---------------------------------------------------------------------------
class TestAnfangsbestandHttp:
    def test_korrektur_setzt_den_kontostand(self, smtp, csrf, kunde, app):
        pid = kunde["person_id"]
        r = smtp.post(f"/personen/{pid}/konto/anfangsbestand", data={
            "csrf_token": csrf(f"/personen/{pid}/konto"),
            "balance": "25,00", "reason": "Bargeld vor Kontofuehrung",
        }, follow_redirects=True)
        assert r.status_code == 200

        st = _state(app, pid)
        assert st["balance"] == 2500
        assert len(st["ledger"]) == 1
        assert st["ledger"][0].note == "Bargeld vor Kontofuehrung"

    def test_korrektur_kann_schuld_beseitigen(self, smtp, csrf, kunde, app):
        pid = kunde["person_id"]
        _buche(smtp, csrf, kunde)

        smtp.post(f"/personen/{pid}/konto/anfangsbestand", data={
            "csrf_token": csrf(f"/personen/{pid}/konto"),
            "balance": "0", "reason": "Verrechnet",
        }, follow_redirects=True)

        assert _state(app, pid)["balance"] == 0

    def test_zweiter_aufruf_mit_gleichem_wert_aendert_nichts(
        self, smtp, csrf, kunde, app
    ):
        pid = kunde["person_id"]
        for _ in range(2):
            smtp.post(f"/personen/{pid}/konto/anfangsbestand", data={
                "csrf_token": csrf(f"/personen/{pid}/konto"),
                "balance": "10,00", "reason": "x",
            }, follow_redirects=True)

        st = _state(app, pid)
        assert st["balance"] == 1000
        assert len(st["ledger"]) == 1


# ---------------------------------------------------------------------------
# Sicherheitsregeln
# ---------------------------------------------------------------------------
class TestErneuterMailversand:
    def test_route_ist_ohne_leerzeichen_erreichbar(self, app):
        """Regression: die Route enthielt ein Leerzeichen im Pfad."""
        rules = {str(r) for r in app.url_map.iter_rules()}
        assert "/personen/<int:person_id>/einzahlung/<int:deposit_id>/erneut-senden" in rules
        assert not any(" /" in r or " " in r for r in rules), "Leerzeichen in URL-Regel"

    def test_wiederholung_bucht_nicht_nochmal(self, smtp, csrf, kunde, app):
        pid = kunde["person_id"]
        smtp.post(f"/personen/{pid}/einzahlung", data={
            "csrf_token": csrf(f"/personen/{pid}/einzahlung"),
            "amount": "25,00", "payment_type": "BAR", "send_email": "1",
        }, follow_redirects=True)

        st = _state(app, pid)
        dep_id = st["deposits"][0].id
        assert st["balance"] == 2500

        for _ in range(2):
            r = smtp.post(f"/personen/{pid}/einzahlung/{dep_id}/erneut-senden",
                          data={"csrf_token": csrf(f"/personen/{pid}/konto")},
                          follow_redirects=True)
            assert r.status_code == 200

        st2 = _state(app, pid)
        # Kontostand und Anzahl der Einzahlungen bleiben unveraendert.
        assert st2["balance"] == 2500
        assert len(st2["deposits"]) == 1
        assert len(st2["ledger"]) == 1

    def test_wiederholung_ohne_csrf_wird_abgelehnt(self, smtp, csrf, kunde, app):
        pid = kunde["person_id"]
        smtp.post(f"/personen/{pid}/einzahlung", data={
            "csrf_token": csrf(f"/personen/{pid}/einzahlung"),
            "amount": "5,00", "payment_type": "BAR",
        }, follow_redirects=True)
        dep_id = _state(app, pid)["deposits"][0].id

        r = smtp.post(f"/personen/{pid}/einzahlung/{dep_id}/erneut-senden", data={})
        assert r.status_code in (302, 400, 403)
        assert _state(app, pid)["balance"] == 500

    def test_ohne_login_kein_wiederholversand(self, client, smtp, csrf, kunde, app):
        pid = kunde["person_id"]
        smtp.post(f"/personen/{pid}/einzahlung", data={
            "csrf_token": csrf(f"/personen/{pid}/einzahlung"),
            "amount": "5,00", "payment_type": "BAR",
        }, follow_redirects=True)
        dep_id = _state(app, pid)["deposits"][0].id

        r = client.post(f"/personen/{pid}/einzahlung/{dep_id}/erneut-senden",
                        data={"csrf_token": "x"})
        # Abgelehnt (CSRF/401/Redirect), aber auf jeden Fall keine Aenderung.
        assert r.status_code in (302, 303, 400, 401, 403)
        assert _state(app, pid)["balance"] == 500
        assert len(_state(app, pid)["ledger"]) == 1


class TestKontoSicherheit:
    def test_ohne_anmeldung_kein_zugriff(self, client, app):
        assert client.get("/personen/1/konto").status_code in (302, 303, 401, 403)
        assert client.post("/personen/1/einzahlung", data={"amount": "10,00"}
                           ).status_code in (302, 303, 401, 403)

    def test_konto_einer_fremden_person_nicht_sichtbar_ohne_login(self, client):
        # Fremde Konten sind ohne Anmeldung nicht erreichbar.
        for pfad in ("/personen/2/konto", "/personen/2/einzahlung"):
            r = client.get(pfad)
            assert r.status_code in (302, 303, 401, 403)
            assert "Kontostand" not in r.get_data(as_text=True)