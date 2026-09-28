"""HTTP-Ebene: Setup, Login, CRUD, Verzehr, Einstellungen, Abrechnung, System."""

from __future__ import annotations

import re

import pytest

from app.models import (
    AdminUser,
    Consumption,
    ConsumptionStatus,
    Invoice,
    InvoiceStatus,
    PaymentStatus,
    Person,
    Product,
    SetupState,
)

pytestmark = pytest.mark.usefixtures("app")


def _token(client, path="/login"):
    html = client.get(path).get_data(as_text=True)
    match = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', html)
    assert match, f"kein CSRF-Token auf {path}"
    return match.group(1)


# ---------------------------------------------------------------------------
# Setup-Assistent
# ---------------------------------------------------------------------------
class TestSetup:
    def test_setup_leitet_von_der_loginseite_um(self, client):
        r = client.get("/login", follow_redirects=True)
        assert r.status_code == 200
        assert "/setup" in r.request.path or "Einrichtung" in r.get_data(as_text=True)

    def test_index_zeigt_ersten_schritt(self, client):
        r = client.get("/setup")
        assert r.status_code == 302
        assert "/setup/1" in r.headers["Location"]

    def test_sprueche_sind_gesperrt(self, client, csrf):
        client.post("/setup/1", data={"csrf_token": csrf("/setup/1")})
        r = client.get("/setup/5", follow_redirects=False)
        assert r.status_code == 302
        assert "/setup/2" in r.headers["Location"]

    def test_vollstaendiger_durchlauf(self, app, client, csrf):
        steps = [
            ("/setup/1", {}),
            ("/setup/2", {
                "admin_username": "boss",
                "admin_email": "boss@example.com",
                "admin_password": "EinSicheres!2026",
                "admin_password_confirm": "EinSicheres!2026",
                "display_name": "Boss",
            }),
            ("/setup/3", {
                "paypal_me_username": "MEINPAYPAL",
                "currency": "EUR",
                "paypal_base_url": "https://www.paypal.me",
            }),
            ("/setup/4", {
                "smtp_host": "smtp.example.com",
                "smtp_port": "587",
                "smtp_encryption": "starttls",
                "smtp_username": "mail@example.com",
                "mail_from_address": "a@example.com",
                "mail_from_name": "Verzehr",
                "action": "save",
            }),
            ("/setup/5", {
                "auto_billing_enabled": "1",
                "auto_billing_time": "23:00",
                "auto_catchup_enabled": "1",
                "action": "save",
            }),
            ("/setup/6", {}),
        ]
        for path, data in steps:
            r = client.post(path, data={"csrf_token": csrf(path), **data},
                            follow_redirects=False)
            assert r.status_code == 302, f"{path} -> {r.status_code}"

        with app.app_context():
            from app.db import get_session
            from app.settings_service import Settings

            db = get_session()
            assert db.get(SetupState, 1).completed is True
            assert db.query(AdminUser).count() == 1
            assert Settings(db).paypal_username == "MEINPAYPAL"

    def test_setup_ist_nach_abschluss_gesperrt(self, admin, csrf):
        r = admin.get("/setup/1", follow_redirects=False)
        assert r.status_code in (302, 404)

    def test_admin_darf_nicht_doppelt_angelegt_werden(self, client, csrf, ctx):
        client.post("/setup/1", data={"csrf_token": csrf("/setup/1")})
        data = {
            "admin_username": "boss", "admin_email": "boss@example.com",
            "admin_password": "EinSicheres!2026",
            "admin_password_confirm": "EinSicheres!2026",
        }
        client.post("/setup/2", data={"csrf_token": csrf("/setup/2"), **data})
        r = client.post("/setup/2", data={"csrf_token": csrf("/setup/2"), **data})
        assert r.status_code == 200
        assert "bereits" in r.get_data(as_text=True).lower()
        with ctx() as db:
            assert db.query(AdminUser).count() == 1

    @pytest.mark.parametrize("step,payload,bad_value", [
        (2, {"admin_username": "boss", "admin_email": "boss@example.com",
             "admin_password": "EinSicheres!2026",
             "admin_password_confirm": "EinSicheres!2026"}, "admin_email"),
        (3, {"paypal_me_username": "TEST", "currency": "EUR",
             "paypal_base_url": "https://www.paypal.me"}, "currency"),
    ])
    def test_fehlerhaefte_eingabe_bleibt_im_schritt(self, client, csrf, ctx,
                                                   step, payload, bad_value):
        """A rejected POST must not advance the wizard nor drop the message."""
        client.post("/setup/1", data={"csrf_token": csrf("/setup/1")})
        if step == 3:
            client.post("/setup/2", data={"csrf_token": csrf("/setup/2"), **{
                "admin_username": "boss", "admin_email": "boss@example.com",
                "admin_password": "EinSicheres!2026",
                "admin_password_confirm": "EinSicheres!2026"}})
        payload = dict(payload)
        payload[bad_value] = "ungueltig!!"
        r = client.post(f"/setup/{step}", data={"csrf_token": csrf(f"/setup/{step}"),
                                                **payload})
        assert r.status_code == 200, f"Schritt {step} -> {r.status_code}"
        html = r.get_data(as_text=True)
        assert f'step-{step} active' in html or f"Schritt {step}" in html
        # the error notice is rendered
        assert "notice-danger" in html
        # the wizard did not move on
        with ctx() as db:
            assert db.get(SetupState, 1).current_step == step


# ---------------------------------------------------------------------------
# Authentifizierung
# ---------------------------------------------------------------------------
class TestAuth:
    def test_login_mit_falschem_passwort(self, admin, csrf):
        admin.post("/logout", data={"csrf_token": csrf("/abrechnungen/")})
        r = admin.post("/login", data={
            "identifier": "testadmin", "password": "falsch",
            "csrf_token": csrf("/login")})
        assert r.status_code in (200, 401)
        assert "abrechnungen" not in r.request.path

    def test_logout_beendet_die_sitzung(self, admin, csrf):
        assert admin.get("/abrechnungen/").status_code == 200
        admin.post("/logout", data={"csrf_token": csrf("/abrechnungen/")})
        r = admin.get("/abrechnungen/", follow_redirects=False)
        assert r.status_code == 302
        assert "/login" in r.headers["Location"]

    def test_ohne_login_erst_zum_setup(self, client):
        # without a completed setup, protected pages point at the wizard
        for path in ("/abrechnungen/", "/personen/", "/produkte/",
                     "/einstellungen/paypal", "/system/"):
            r = client.get(path, follow_redirects=False)
            assert r.status_code == 302, path
            assert "/setup" in r.headers["Location"], path

    def test_seiten_erfordern_login_nach_setup(self, app, admin):
        fresh = app.test_client()
        for path in ("/abrechnungen/", "/personen/", "/produkte/",
                     "/einstellungen/paypal", "/system/", "/verzehr/erfassen"):
            r = fresh.get(path, follow_redirects=False)
            assert r.status_code == 302, path
            assert "/login" in r.headers["Location"], path


# ---------------------------------------------------------------------------
# Datenbankwechsel in Schritt 1
# ---------------------------------------------------------------------------
class TestDatenbankwechsel:
    def test_umstellen_auf_andere_sqlite_datei(self, free_app, tmp_path):
        from app.db import get_session

        client = free_app.test_client()
        target = tmp_path / "neu" / "payment.db"
        assert client.get("/setup/1").status_code == 200
        token = _token(client, "/setup/1")
        r = client.post("/setup/1", data={
            "csrf_token": token,
            "action": "change",
            "database_url": f"sqlite:///{target.as_posix()}",
        }, follow_redirects=False)
        assert r.status_code == 302, r.get_data(as_text=True)[:400]
        assert "/setup/2" in r.headers["Location"]
        assert target.exists()

        # the wizard state lives in the new database now
        with free_app.app_context():
            assert get_session().get(SetupState, 1) is not None

    def test_verbindung_ueberlebt_einen_neustart(self, free_app, tmp_env, tmp_path):
        """The chosen URL is persisted in the secrets store."""
        from pathlib import Path

        from app.config import Config

        target = tmp_path / "dauerhaft.db"
        client = free_app.test_client()
        client.post("/setup/1", data={
            "csrf_token": _token(client, "/setup/1"),
            "action": "change",
            "database_url": f"sqlite:///{target.as_posix()}",
        })
        assert target.exists()

        # a fresh Config picks the stored value up again
        import os

        os.environ.pop("DATABASE_URL", None)
        assert Config().DATABASE_URL == f"sqlite:///{target.as_posix()}"
        assert (Path(tmp_env) / "secrets.env").exists()

    def test_gepinnete_umgebungsvariable_laesst_sich_nicht_aendern(self, app):
        client = app.test_client()
        r = client.post("/setup/1", data={
            "csrf_token": _token(client, "/setup/1"),
            "action": "change",
            "database_url": "sqlite:///C:/temp/anders.db",
        })
        assert r.status_code == 200
        html = r.get_data(as_text=True)
        assert "Umgebungsvariable" in html
        assert "durch Umgebungsvariable gesetzt" in html

    def test_pfad_fuer_sqlite_ist_pflicht(self, client, csrf):
        r = client.post("/setup/1", data={
            "csrf_token": csrf("/setup/1"),
            "action": "change",
            "database_url": "sqlite://",
        })
        assert r.status_code == 200
        html = r.get_data(as_text=True)
        assert "Dateipfad" in html
        assert "notice-danger" in html

    def test_unvollstaendige_postgres_url_wird_abgelehnt(self, client, csrf):
        r = client.post("/setup/1", data={
            "csrf_token": csrf("/setup/1"),
            "action": "change",
            "database_url": "postgresql://user@host",
        })
        assert r.status_code == 200
        assert "Datenbankname" in r.get_data(as_text=True)

    def test_passwort_der_url_wird_nie_angezeigt(self, free_app, tmp_path):
        client = free_app.test_client()
        target = tmp_path / "ander.db"
        r = client.post("/setup/1", data={
            "csrf_token": _token(client, "/setup/1"),
            "action": "change",
            "database_url": f"sqlite:///{target.as_posix()}",
        })
        assert r.status_code == 302
        html = client.get("/setup/1").get_data(as_text=True)
        assert "password" not in html.lower()
        assert target.name in html
        assert "sqlite://" in html

    def test_csrf_ist_pflicht(self, admin):
        r = admin.post("/personen/neu", data={"first_name": "X", "last_name": "Y"})
        assert r.status_code == 400

    def test_passwort_aendern(self, admin, csrf):
        r = admin.post("/profil/password", data={
            "csrf_token": csrf("/profil/password"),
            "current_password": "Sicher!2026x",
            "new_password": "NeuesPasswort!2026",
            "confirm_password": "NeuesPasswort!2026",
        }, follow_redirects=False)
        assert r.status_code == 302
        admin.post("/logout", data={"csrf_token": csrf("/abrechnungen/")})
        r = admin.post("/login", data={
            "identifier": "testadmin", "password": "NeuesPasswort!2026",
            "csrf_token": csrf("/login")}, follow_redirects=False)
        assert r.status_code == 302

    def test_falsches_aktuelles_passwort_wird_abgelehnt(self, admin, csrf):
        r = admin.post("/profil/password", data={
            "csrf_token": csrf("/profil/password"),
            "current_password": "falsch",
            "new_password": "NeuesPasswort!2026",
            "confirm_password": "NeuesPasswort!2026",
        }, follow_redirects=True)
        assert r.status_code == 200
        # the old password still works
        r = admin.post("/profil/password", data={
            "csrf_token": csrf("/profil/password"),
            "current_password": "Sicher!2026x",
            "new_password": "AnderesPasswort!2026",
            "confirm_password": "AnderesPasswort!2026",
        }, follow_redirects=False)
        assert r.status_code == 302

    def test_healthz(self, admin):
        r = admin.get("/healthz")
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# Personen und Produkte
# ---------------------------------------------------------------------------
class TestCrud:
    def test_person_anlegen_und_anzeigen(self, admin, csrf, app):
        r = admin.post("/personen/neu", data={
            "csrf_token": csrf("/personen/neu"), "first_name": "Anna",
            "last_name": "Schmidt", "email": "anna@example.com"}, follow_redirects=True)
        assert r.status_code == 200
        assert "Anna" in r.get_data(as_text=True)
        with app.app_context():
            from app.db import get_session
            assert get_session().query(Person).count() == 1

    def test_person_ohne_vorname_wird_abgelehnt(self, admin, csrf):
        r = admin.post("/personen/neu", data={
            "csrf_token": csrf("/personen/neu"), "last_name": "X"},
            follow_redirects=True)
        assert r.status_code == 200
        assert "anna" not in r.get_data(as_text=True).lower() or "fehler" in \
            r.get_data(as_text=True).lower()

    def test_produkt_anlegen_mit_deutschem_komma(self, admin, csrf, app):
        admin.post("/produkte/neu", data={
            "csrf_token": csrf("/produkte/neu"), "name": "Cola",
            "price": "2,00", "category": "Getraenke"}, follow_redirects=True)
        with app.app_context():
            from app.db import get_session
            product = get_session().query(Product).filter_by(name="Cola").one()
            assert product.price_cents == 200

    def test_negativer_preis_wird_abgelehnt(self, admin, csrf, app):
        r = admin.post("/produkte/neu", data={
            "csrf_token": csrf("/produkte/neu"), "name": "Negativ",
            "price": "-5,00"}, follow_redirects=True)
        assert "nicht negativ" in r.get_data(as_text=True)
        with app.app_context():
            from app.db import get_session
            assert get_session().query(Product).filter_by(name="Negativ").count() == 0

    def test_person_deaktivieren(self, admin, csrf, app):
        admin.post("/personen/neu", data={
            "csrf_token": csrf("/personen/neu"), "first_name": "Erika",
            "last_name": "Musterfrau", "email": "e@example.com"})
        with app.app_context():
            from app.db import get_session
            db = get_session()
            pid = db.query(Person).one().id
        admin.post(f"/personen/{pid}/status", data={"csrf_token": csrf("/personen/")})
        with app.app_context():
            from app.db import get_session
            assert get_session().get(Person, pid).is_active is False


# ---------------------------------------------------------------------------
# Verzehr
# ---------------------------------------------------------------------------
class TestVerzehr:
    @pytest.fixture()
    def setup_data(self, admin, csrf, app):
        admin.post("/personen/neu", data={
            "csrf_token": csrf("/personen/neu"), "first_name": "Max",
            "last_name": "Mustermann", "email": "max@example.com"})
        admin.post("/produkte/neu", data={
            "csrf_token": csrf("/produkte/neu"), "name": "Cola", "price": "2,00"})
        with app.app_context():
            from app.db import get_session
            db = get_session()
            return {
                "person_id": db.query(Person).one().id,
                "product_id": db.query(Product).one().id,
            }

    def test_schnellverzehr(self, admin, csrf, setup_data, app):
        r = admin.post("/verzehr/schnell", data={
            "csrf_token": csrf(f"/verzehr/erfassen?person_id={setup_data['person_id']}"),
            "person_id": setup_data["person_id"],
            "product_id": setup_data["product_id"],
            "quantity": "3"}, follow_redirects=True)
        assert r.status_code == 200
        with app.app_context():
            from app.db import get_session
            row = get_session().query(Consumption).one()
            assert row.total_cents == 600
            assert row.unit_price_cents == 200
            assert row.status == ConsumptionStatus.OFFEN
            assert row.invoice_id is None

    def test_stornieren_und_wiederherstellen(self, admin, csrf, setup_data, app):
        admin.post("/verzehr/schnell", data={
            "csrf_token": csrf(f"/verzehr/erfassen?person_id={setup_data['person_id']}"),
            "person_id": setup_data["person_id"],
            "product_id": setup_data["product_id"], "quantity": "1"})
        with app.app_context():
            from app.db import get_session
            cid = get_session().query(Consumption).one().id

        admin.post(f"/verzehr/{cid}/stornieren", data={"csrf_token": csrf("/verzehr/uebersicht")})
        with app.app_context():
            from app.db import get_session
            assert get_session().get(Consumption, cid).status == ConsumptionStatus.STORNIERT

        admin.post(f"/verzehr/{cid}/bestaetigen", data={"csrf_token": csrf("/verzehr/uebersicht")})
        with app.app_context():
            from app.db import get_session
            assert get_session().get(Consumption, cid).status == ConsumptionStatus.OFFEN

    def test_unbekanntes_produkt_wird_abgelehnt(self, admin, csrf, setup_data, app):
        admin.post("/verzehr/schnell", data={
            "csrf_token": csrf(f"/verzehr/erfassen?person_id={setup_data['person_id']}"),
            "person_id": setup_data["person_id"],
            "product_id": "99999", "quantity": "1"})
        with app.app_context():
            from app.db import get_session
            assert get_session().query(Consumption).count() == 0

    def test_uebersicht_zeigt_offene_buchungen(self, admin, csrf, setup_data):
        admin.post("/verzehr/schnell", data={
            "csrf_token": csrf(f"/verzehr/erfassen?person_id={setup_data['person_id']}"),
            "person_id": setup_data["person_id"],
            "product_id": setup_data["product_id"], "quantity": "2"})
        r = admin.get("/verzehr/uebersicht")
        assert r.status_code == 200
        assert "Cola" in r.get_data(as_text=True)


# ---------------------------------------------------------------------------
# Einstellungen
# ---------------------------------------------------------------------------
class TestSettings:
    def test_paypal_speichern(self, admin, csrf, app):
        r = admin.post("/einstellungen/paypal", data={
            "csrf_token": csrf("/einstellungen/paypal"),
            "paypal_me_username": "MEINNAME", "currency": "EUR",
            "paypal_base_url": "https://www.paypal.me"}, follow_redirects=True)
        assert r.status_code == 200
        with app.app_context():
            from app.db import get_session
            from app.settings_service import Settings
            assert Settings(get_session()).paypal_username == "MEINNAME"

    def test_ungueltige_uhrzeit_wird_abgelehnt(self, admin, csrf, app):
        admin.post("/einstellungen/billing", data={
            "csrf_token": csrf("/einstellungen/billing"),
            "auto_billing_enabled": "1", "auto_billing_time": "23:30",
            "timezone": "Europe/Berlin", "auto_catchup_enabled": "1",
            "invoice_number_prefix": "RE-"}, follow_redirects=True)
        r = admin.post("/einstellungen/billing", data={
            "csrf_token": csrf("/einstellungen/billing"),
            "auto_billing_time": "99:99", "timezone": "Europe/Berlin",
            "invoice_number_prefix": "RE-"}, follow_redirects=True)
        assert r.status_code == 200
        with app.app_context():
            from app.db import get_session
            from app.settings_service import Settings
            assert Settings(get_session()).auto_billing_time == "23:30"

    @pytest.mark.parametrize("tab", ["paypal", "billing", "email", "smtp", "system"])
    def test_alle_tabs_rendern(self, admin, tab):
        assert admin.get(f"/einstellungen/{tab}").status_code == 200


# ---------------------------------------------------------------------------
# SMTP-Konfiguration
# ---------------------------------------------------------------------------
class TestSmtpKonfiguration:
    """Regression: Gmail-App-Passwoerter werden in Vierergruppen angezeigt."""

    @pytest.mark.parametrize(
        "rohdaten",
        [
            "abcd efgh ijkl mnop",       # so zeigt Google es an
            " abcd efgh ijkl mnop ",     # mit Randleerzeichen
            "abcdefghijklmnop",          # ohne Leerzeichen
            "abcd\tefgh\nijkl  mnop",    # verirrte Leerzeichen
        ],
    )
    def test_app_passwort_ohne_leerzeichen(self, rohdaten):
        from app.services.mail_factory import normalize_password
        assert normalize_password(rohdaten) == "abcdefghijklmnop"

    @pytest.mark.parametrize("leer", [None, "", "   "])
    def test_leeres_passwort(self, leer):
        from app.services.mail_factory import normalize_password
        assert normalize_password(leer) == ""

    def test_passwort_aus_secrets_wird_normalisiert(self, app):
        from app.services.mail_factory import smtp_config_from_settings
        from app.settings_service import Settings

        class _Store:
            def get(self, key, default=None):
                return "wxyz 1234 abcd efgh"

        with app.app_context():
            from app.db import get_session
            cfg = smtp_config_from_settings(Settings(get_session()), _Store())
        assert cfg.password == "wxyz1234abcdefgh"

    def test_ohne_secrets_ist_das_passwort_leer(self, app):
        from app.services.mail_factory import smtp_config_from_settings
        from app.settings_service import Settings
        with app.app_context():
            from app.db import get_session
            cfg = smtp_config_from_settings(Settings(get_session()), None)
        assert cfg.password == ""

    def test_gmail_konfiguration_ergaenzt_smtp(self, admin, csrf, app):
        admin.post("/einstellungen/smtp", data={
            "csrf_token": csrf("/einstellungen/smtp"),
            "smtp_host": "smtp.gmail.com", "smtp_port": "587",
            "smtp_encryption": "starttls",
            "smtp_username": "jona.snoek@gmail.com",
            "mail_from_name": "Verzehrabrechnung",
            "mail_from_address": "jona.snoek@gmail.com",
        }, follow_redirects=True)
        with app.app_context():
            from app.db import get_session
            from app.services.mail_factory import smtp_config_from_settings
            from app.settings_service import Settings
            cfg = smtp_config_from_settings(Settings(get_session()), None)
            assert cfg.host == "smtp.gmail.com"
            assert cfg.port == 587
            assert cfg.encryption == "starttls"
            assert cfg.username == "jona.snoek@gmail.com"
            assert cfg.from_address == "jona.snoek@gmail.com"

    def test_smtp_seite_zeigt_provider_vorlagen(self, admin):
        r = admin.get("/einstellungen/smtp")
        assert r.status_code == 200
        text = r.get_data(as_text=True)
        assert "smtp.gmail.com" in text
        assert "smtp.office365.com" in text
        assert "2-Schritt-Verifizierung" in text


# ---------------------------------------------------------------------------
# SMTP-Passwort anzeigen
# ---------------------------------------------------------------------------
class TestSmtpPasswortAnzeigen:
    URL = "/einstellungen/smtp/password-reveal"

    def _set_password(self, admin, csrf, value):
        admin.post("/einstellungen/smtp", data={
            "csrf_token": csrf("/einstellungen/smtp"),
            "smtp_host": "smtp.gmail.com", "smtp_port": "587",
            "smtp_encryption": "starttls",
            "smtp_username": "konto@gmail.com",
            "smtp_password": value,
            "mail_from_name": "Verzehrabrechnung",
            "mail_from_address": "konto@gmail.com",
        }, follow_redirects=True)

    def test_anzeige_nur_per_post(self, admin, csrf):
        """Ein GET darf das Passwort niemals ausliefern."""
        self._set_password(admin, csrf, "abcd efgh ijkl mnop")
        r = admin.get(self.URL)
        assert r.status_code == 405
        assert "abcd" not in r.get_data(as_text=True)

    def test_ohne_csrf_wird_abgewiesen(self, admin, csrf, app):
        self._set_password(admin, csrf, "abcd efgh ijkl mnop")
        r = admin.post(self.URL, data={}, follow_redirects=False)
        assert r.status_code in (400, 403, 302)
        if r.status_code == 302:
            # Umgeleitet heisst: nicht aufgedeckt, nur Fehlermeldung.
            assert "abcd" not in r.get_data(as_text=True)

    def test_anzeige_zeigt_gespeichertes_passwort(self, admin, csrf):
        self._set_password(admin, csrf, "qdxkjsdkemuexahj")
        r = admin.post(self.URL, data={
            "csrf_token": csrf("/einstellungen/smtp")}, follow_redirects=True)
        assert r.status_code == 200
        assert "qdxkjsdkemuexahj" in r.get_data(as_text=True)

    def test_anzeige_wird_im_audit_protokolliert(self, admin, csrf, app):
        self._set_password(admin, csrf, "abcd efgh ijkl mnop")
        admin.post(self.URL, data={
            "csrf_token": csrf("/einstellungen/smtp")}, follow_redirects=True)
        with app.app_context():
            from app.db import get_session
            from sqlalchemy import select
            from app.models import AuditLog
            rows = get_session().execute(
                select(AuditLog).where(AuditLog.action == "smtp.password_reveal")
            ).scalars().all()
            assert rows, "Anzeigen des Passworts muss protokolliert werden"
            # Der Audit-Eintrag darf das Passwort selbst nicht enthalten.
            assert "abcdefghijklmnop" not in (rows[-1].detail or "")

    def test_ohne_passwort_keine_anzeige(self, admin, csrf):
        r = admin.post(self.URL, data={
            "csrf_token": csrf("/einstellungen/smtp")}, follow_redirects=True)
        assert r.status_code == 200
        assert "kein SMTP-Passwort" in r.get_data(as_text=True)

    def test_maskierte_vorschau_zeigt_nur_letzte_vier(self, admin, csrf):
        self._set_password(admin, csrf, "abcd efgh ijkl mnop")
        text = admin.get("/einstellungen/smtp").get_data(as_text=True)
        assert "mnop" in text          # letzte vier Zeichen sind sichtbar
        assert "abcdefgh" not in text  # der Rest nicht

    def test_neues_passwort_ueberschreibt_ohne_loeschen(self, admin, csrf):
        self._set_password(admin, csrf, "erstes_ABCD")
        self._set_password(admin, csrf, "zweites_WXYZ")
        # Die Vorschau zeigt die letzten vier Zeichen - damit ist erkennbar,
        # dass der neue Wert den alten ersetzt hat.
        text = admin.get("/einstellungen/smtp").get_data(as_text=True)
        assert "WXYZ" in text
        assert "ABCD" not in text
        r = admin.post(self.URL, data={
            "csrf_token": csrf("/einstellungen/smtp")}, follow_redirects=True)
        shown = r.get_data(as_text=True)
        assert "zweites_WXYZ" in shown
        assert "erstes_ABCD" not in shown

    def test_nicht_angemeldet_kommt_nicht_durch(self, app, client):
        r = client.post(self.URL, data={})
        assert r.status_code in (302, 401, 403)
        assert "Passwort" not in r.get_data(as_text=True)


# ---------------------------------------------------------------------------
# Kommentar hinter einem Wert in .env
# ---------------------------------------------------------------------------
class TestEnvKommentarRobust:
    """Regression fuer den Produktionsfehler mit dem Passwort.

    systemd laedt .env ueber EnvironmentFile= und schneidet keinen Kommentar
    hinter dem Wert ab. "SMTP_PASSWORD=   # GEHEIM - niemals committen" wird
    dadurch zur echten Umgebungsvariable und schlaegt jede Eingabe im
    Formular - das Passwort war dadurch nicht mehr aenderbar.
    """

    @pytest.fixture()
    def store(self, tmp_path, monkeypatch):
        from app.secrets_store import SecretsStore
        for var in ("SMTP_PASSWORD", "SECRET_KEY", "DB_ENCRYPTION_KEY"):
            monkeypatch.delenv(var, raising=False)
        s = SecretsStore(tmp_path / "secrets.env")
        s.set("SMTP_PASSWORD", "echtespasswort")
        return s

    def test_kommentar_in_umgebung_ist_nicht_gesetzt(self, store, monkeypatch):
        monkeypatch.setenv("SMTP_PASSWORD", "                 # GEHEIM - niemals committen")
        # Der Kommentar darf nicht als "ueberschreibend" gelten ...
        assert store.is_overridden_by_env("SMTP_PASSWORD") is False
        # ... damit die Datei wieder gewinnt und das Passwort aenderbar bleibt.
        assert store.get("SMTP_PASSWORD") == "echtespasswort"

    def test_ohne_dateiwert_gilt_kommentar_als_nicht_gesetzt(self, tmp_path, monkeypatch):
        from app.secrets_store import SecretsStore
        monkeypatch.delenv("SMTP_PASSWORD", raising=False)
        leer = SecretsStore(tmp_path / "leer.env")
        monkeypatch.setenv("SMTP_PASSWORD", "   # GEHEIM - niemals committen")
        assert leer.has("SMTP_PASSWORD") is False
        assert leer.get("SMTP_PASSWORD") is None

    @pytest.mark.parametrize(
        "wert",
        [
            "# GEHEIM - niemals committen",
            "   # comment",
            "#",
            "",
            "   ",
            None,
            "changeme",
            "********",
        ],
    )
    def test_platzhalter_und_kommentare(self, wert):
        from app.secrets_store import is_placeholder
        assert is_placeholder(wert) is True

    @pytest.mark.parametrize("wert", ["abcd efgh ijkl mnop", "qdxkjsdkemuexahj", "x"])
    def test_echte_werte_sind_keine_platzhalter(self, wert):
        from app.secrets_store import is_placeholder
        assert is_placeholder(wert) is False

    def test_echte_umgebungsvariable_gewinnt_weiterhin(self, store, monkeypatch):
        monkeypatch.setenv("SMTP_PASSWORD", "absichtlicherwert")
        assert store.is_overridden_by_env("SMTP_PASSWORD") is True
        assert store.get("SMTP_PASSWORD") == "absichtlicherwert"

    def test_passwort_laesst_sich_trotz_kommentar_aendern(self, admin, csrf, monkeypatch):
        monkeypatch.setenv("SMTP_PASSWORD", "   # altes .env.example")
        TestSmtpPasswortAnzeigen()._set_password(admin, csrf, "neuespasswort")
        r = admin.post("/einstellungen/smtp/password-reveal", data={
            "csrf_token": csrf("/einstellungen/smtp")}, follow_redirects=True)
        assert "neuespasswort" in r.get_data(as_text=True)

    def test_env_example_hat_keine_inline_kommentare(self):
        """Sonst faellt der Fehler bei jedem frischen Server erneut auf."""
        from pathlib import Path
        root = Path(__file__).resolve().parents[1]
        offenders = []
        for name in (".env.example",):
            for i, raw in enumerate((root / name).read_text(encoding="utf-8").splitlines(), 1):
                line = raw.rstrip()
                if line.lstrip().startswith("#") or "=" not in line:
                    continue
                _, _, value = line.partition("=")
                if "#" in value:
                    offenders.append(f"{name}:{i} {line.split('=')[0]}")
        assert not offenders, "Inline-Kommentare gefunden: " + ", ".join(offenders)


# ---------------------------------------------------------------------------
# Fehlergrund beim Rechnungsversand
# ---------------------------------------------------------------------------
class TestVersandfehlerSichtbar:
    """Die Meldung muss den echten Grund nennen, nicht nur 'SMTP-Fehler'.

    Produktion: die Test-Mail kam an, die Rechnung nicht, und die Oberflaeche
    zeigte nur "SMTP-Fehler". Der Grund stand in items[].error, wurde aber
    nie ausgegeben.
    """

    @pytest.fixture()
    def result(self):
        from datetime import date

        from app.services.billing import BillingResult, PersonBillingResult

        def _make(errors):
            r = BillingResult(period_date=date(2026, 1, 1), trigger="manual")
            for i, err in enumerate(errors):
                r.items.append(PersonBillingResult(
                    person_id=i + 1,
                    person_name=f"Person {i + 1}",
                    email="p@example.com",
                    status="fehlgeschlagen",
                    error=err,
                ))
            r.emails_failed = len(errors)
            return r
        return _make

    def test_einzigener_grund_wird_genannt(self, result):
        r = result(["Ungueltige Empfaengeradresse"])
        assert r.first_error() == "Ungueltige Empfaengeradresse"

    def test_mehrere_gruende_gezaehlt(self, result):
        r = result(["Ungueltige Empfaengeradresse", "PayPal.Me-Benutzername fehlt"])
        assert r.first_error() == "Ungueltige Empfaengeradresse (+1 weitere)"

    def test_ohne_fehler_leer(self, result):
        assert result([]).first_error() == ""

    def test_gleiche_gruende_nicht_doppelt(self, result):
        assert len(result(["Gleicher Fehler", "Gleicher Fehler"]).errors()) == 1

    def test_ohne_email_zeigt_grund_statt_smtp_fehler(self, admin, csrf, db, configured, book):
        """Ohne Empfaengeradresse darf kein SMTP-Fehler behauptet werden."""
        from app.models import Person
        person = db.get(Person, configured["person_id"])
        book(configured["person_id"], "Spezi", 200)
        person.email = ""
        db.commit()
        url = f"/abrechnungen/person/{person.id}"
        r = admin.post(url, data={"csrf_token": csrf(url)}, follow_redirects=True)
        text = r.get_data(as_text=True)
        assert "Ungueltige Empfaengeradresse" in text
        assert "SMTP-Fehler" not in text

    def test_fehlender_paypal_handle_wird_genannt(self, admin, csrf, db, configured, book):
        from app.models import Person
        book(configured["person_id"], "Spezi", 200)
        configured["settings"].set("paypal_me_username", "")
        db.commit()
        url = f"/abrechnungen/person/{configured['person_id']}"
        r = admin.post(url, data={"csrf_token": csrf(url)}, follow_redirects=True)
        text = r.get_data(as_text=True)
        assert "PayPal" in text
        assert "SMTP-Fehler" not in text

    def test_betrag_bleibt_offen(self, admin, csrf, db, configured, book, balance):
        from app.models import Person
        person = db.get(Person, configured["person_id"])
        book(configured["person_id"], "Spezi", 200)
        person.email = ""
        db.commit()
        vorher = balance(configured["person_id"])
        url = f"/abrechnungen/person/{person.id}"
        admin.post(url, data={"csrf_token": csrf(url)})
        assert balance(configured["person_id"]) == vorher > 0


# ---------------------------------------------------------------------------
# Abrechnung ueber HTTP
# ---------------------------------------------------------------------------
class TestAbrechnungHttp:
    @pytest.fixture()
    def data(self, smtp, csrf, app):
        smtp.post("/personen/neu", data={
            "csrf_token": csrf("/personen/neu"), "first_name": "Max",
            "last_name": "Mustermann", "email": "max@example.com"})
        smtp.post("/produkte/neu", data={
            "csrf_token": csrf("/produkte/neu"), "name": "Spezi", "price": "2,00"})
        with app.app_context():
            from app.db import get_session
            db = get_session()
            return {"person_id": db.query(Person).one().id,
                    "product_id": db.query(Product).one().id}

    def test_manuelle_rechnung_bleibt_offen_bei_smtp_fehler(
        self, smtp, csrf, data, app
    ):
        smtp.post("/verzehr/schnell", data={
            "csrf_token": csrf(f"/verzehr/erfassen?person_id={data['person_id']}"),
            "person_id": data["person_id"],
            "product_id": data["product_id"], "quantity": "2"})
        pid = data["person_id"]
        r = smtp.post(f"/abrechnungen/person/{pid}", data={
            "csrf_token": csrf(f"/abrechnungen/person/{pid}")}, follow_redirects=True)
        assert r.status_code == 200
        assert "offen" in r.get_data(as_text=True).lower()

        with app.app_context():
            from app.db import get_session
            from app.services.billing import open_balance_cents
            db = get_session()
            invoice = db.query(Invoice).one()
            assert invoice.status == InvoiceStatus.FEHLGESCHLAGEN
            assert open_balance_cents(db, pid) == 400
            assert invoice.consumptions[0].status == ConsumptionStatus.OFFEN

    def test_erneut_senden_erhoeht_versuche(self, smtp, csrf, data, app):
        smtp.post("/verzehr/schnell", data={
            "csrf_token": csrf(f"/verzehr/erfassen?person_id={data['person_id']}"),
            "person_id": data["person_id"],
            "product_id": data["product_id"], "quantity": "1"})
        pid = data["person_id"]
        smtp.post(f"/abrechnungen/person/{pid}", data={
            "csrf_token": csrf(f"/abrechnungen/person/{pid}")})
        with app.app_context():
            from app.db import get_session
            iid = get_session().query(Invoice).one().id
        smtp.post(f"/abrechnungen/{iid}/erneut-senden", data={
            "csrf_token": csrf(f"/abrechnungen/{iid}")})
        with app.app_context():
            from app.db import get_session
            assert get_session().get(Invoice, iid).send_attempts >= 2

    def test_bezahlt_markieren_und_zuruecksetzen(self, smtp, csrf, data, app):
        smtp.post("/verzehr/schnell", data={
            "csrf_token": csrf(f"/verzehr/erfassen?person_id={data['person_id']}"),
            "person_id": data["person_id"],
            "product_id": data["product_id"], "quantity": "1"})
        pid = data["person_id"]
        smtp.post(f"/abrechnungen/person/{pid}", data={
            "csrf_token": csrf(f"/abrechnungen/person/{pid}")})
        with app.app_context():
            from app.db import get_session
            iid = get_session().query(Invoice).one().id

        smtp.post(f"/abrechnungen/{iid}/bezahlt", data={
            "csrf_token": csrf(f"/abrechnungen/{iid}")})
        with app.app_context():
            from app.db import get_session
            assert get_session().get(Invoice, iid).payment_status == PaymentStatus.BEZAHLT

        smtp.post(f"/abrechnungen/{iid}/zahlung-offen", data={
            "csrf_token": csrf(f"/abrechnungen/{iid}")})
        with app.app_context():
            from app.db import get_session
            db = get_session()
            invoice = db.get(Invoice, iid)
            assert invoice.payment_status == PaymentStatus.ZAHLUNG_ANGEFORDERT
            assert invoice.paid_at is None

    def test_stornieren_gibt_buchungen_frei(self, smtp, csrf, data, app):
        smtp.post("/verzehr/schnell", data={
            "csrf_token": csrf(f"/verzehr/erfassen?person_id={data['person_id']}"),
            "person_id": data["person_id"],
            "product_id": data["product_id"], "quantity": "1"})
        pid = data["person_id"]
        smtp.post(f"/abrechnungen/person/{pid}", data={
            "csrf_token": csrf(f"/abrechnungen/person/{pid}")})
        with app.app_context():
            from app.db import get_session
            db = get_session()
            iid = db.query(Invoice).one().id

        smtp.post(f"/abrechnungen/{iid}/stornieren", data={
            "csrf_token": csrf(f"/abrechnungen/{iid}")})
        with app.app_context():
            from app.db import get_session
            from app.services.billing import open_balance_cents
            db = get_session()
            assert db.get(Invoice, iid).status == InvoiceStatus.STORNIERT
            assert open_balance_cents(db, pid) == 200
            row = db.query(Consumption).one()
            assert row.invoice_id is None
            assert row.status == ConsumptionStatus.OFFEN

    def test_personenliste_zeigt_offenen_betrag_und_knopf(self, smtp, data):
        r = smtp.get("/personen/")
        assert r.status_code == 200
        assert "Rechnung senden" in r.get_data(as_text=True)
        assert "disabled" in r.get_data(as_text=True)

    def test_abrechnungsindex_rendert(self, smtp, csrf, data):
        smtp.post("/verzehr/schnell", data={
            "csrf_token": csrf(f"/verzehr/erfassen?person_id={data['person_id']}"),
            "person_id": data["person_id"],
            "product_id": data["product_id"], "quantity": "1"})
        assert smtp.get("/abrechnungen/").status_code == 200


# ---------------------------------------------------------------------------
# System
# ---------------------------------------------------------------------------
class TestSystem:
    def test_alle_systemseiten_rendern(self, admin):
        for path in ("/system/", "/system/backups", "/system/backup/restore",
                     "/system/scheduler", "/system/log?kind=audit",
                     "/system/log?kind=email", "/system/log?kind=billing"):
            assert admin.get(path).status_code == 200, path

    def test_backup_erstellen(self, admin, csrf):
        r = admin.post("/system/backup", data={"csrf_token": csrf("/system/")},
                       follow_redirects=True)
        assert "Backup erstellt" in r.get_data(as_text=True)

    def test_exporte(self, admin, csrf):
        admin.post("/personen/neu", data={
            "csrf_token": csrf("/personen/neu"), "first_name": "Max",
            "last_name": "Mustermann", "email": "max@example.com"})
        for path in ("/system/export/verzehr.csv", "/abrechnungen/export.csv",
                     "/personen/1/export.csv"):
            r = admin.get(path)
            assert r.status_code == 200, path
            assert "text/csv" in r.headers["Content-Type"], path

    def test_scheduler_seite_zeigt_zeitpunkt(self, admin):
        html = admin.get("/system/scheduler").get_data(as_text=True)
        assert "Scheduler" in html


# ---------------------------------------------------------------------------
# Fehlerseiten
# ---------------------------------------------------------------------------
class TestFehlerseiten:
    def test_404(self, admin):
        assert admin.get("/gibtsnicht").status_code == 404

    def test_unbekannte_rechnung(self, admin):
        r = admin.get("/abrechnungen/99999", follow_redirects=True)
        assert r.status_code == 200
        assert "nicht gefunden" in r.get_data(as_text=True).lower()
