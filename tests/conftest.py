"""Shared pytest fixtures.

Every test runs against a fresh SQLite database in a temp directory, with the
scheduler disabled and a test secret key.
"""

from __future__ import annotations

import re
from datetime import date, datetime

import pytest

BASE_DATE = date(2026, 9, 28)
# Bookings default to this timestamp. It used to be utcnow(), which silently
# broke every billing test as soon as the real date moved past BASE_DATE: the
# billing run for BASE_DATE then never saw the booking and created no invoice.
BOOKING_TIME = datetime(2026, 9, 28, 12, 0, 0)


@pytest.fixture()
def tmp_env(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'test.db').as_posix()}")
    monkeypatch.setenv("SECRETS_FILE", str(tmp_path / "secrets.env"))
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setenv("SCHEDULER_ENABLED", "0")
    monkeypatch.setenv("SECRET_KEY", "pytest-secret-key")
    monkeypatch.setenv("LOG_LEVEL", "ERROR")
    yield tmp_path


@pytest.fixture()
def app(tmp_env):
    from app import create_app
    from app.config import Config

    application = create_app(Config(), start_scheduler=False)
    application.config.update(TESTING=True)
    return application


@pytest.fixture()
def free_app(monkeypatch, tmp_env):
    """An app whose DATABASE_URL is *not* pinned in the real environment.

    The connection comes from the secrets store instead, which is what a
    normal ``.env``-based deployment looks like - the setup wizard may change
    it there, but not when systemd pins the variable.
    """
    from pathlib import Path

    from app import create_app
    from app.config import Config
    from app.secrets_store import SecretsStore

    monkeypatch.delenv("DATABASE_URL", raising=False)
    SecretsStore(Path(tmp_env) / "secrets.env").set(
        "DATABASE_URL", f"sqlite:///{(Path(tmp_env) / 'test.db').as_posix()}"
    )
    cfg = Config()
    assert cfg.database_url_pinned is False
    application = create_app(cfg, start_scheduler=False)
    application.config.update(TESTING=True)
    return application


@pytest.fixture()
def db(app):
    from app.db import get_session, remove_session

    with app.app_context():
        session = get_session()
        yield session
        session.rollback()
    remove_session()


@pytest.fixture()
def ctx(app, db):
    """``with ctx() as session:`` - a fresh app context around the shared session."""
    import contextlib

    from app.db import get_session, remove_session

    @contextlib.contextmanager
    def _ctx():
        with app.app_context():
            yield get_session()
        remove_session()

    return _ctx


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def mailer(app):
    """Fake mailer. Set ``fail`` to simulate an SMTP outage."""
    from app.mailer import SendResult

    class FakeMailer:
        def __init__(self):
            self.sent: list[tuple] = []
            self.fail = False
            self.error = "SMTP nicht erreichbar"

        def send(self, to, subject, text_body, html_body):
            if self.fail:
                return SendResult(ok=False, error=self.error)
            self.sent.append((to, subject, text_body))
            return SendResult(ok=True)

    with app.app_context():
        pass
    return FakeMailer()


@pytest.fixture()
def configured(app, db):
    """Settings + one active person + products, ready for billing."""
    from app.models import Person, Product
    from app.settings_service import Settings

    settings = Settings(db)
    settings.set("paypal_me_username", "JONASNOEK1")
    settings.set("paypal_base_url", "https://www.paypal.me")
    settings.set("smtp_host", "smtp.example.com")
    settings.set("smtp_port", "587")
    settings.set("mail_from_address", "absender@example.com")
    settings.set("mail_from_name", "Verzehrabrechnung")
    settings.set("auto_billing_enabled", "1")
    settings.set("auto_billing_time", "17:00")
    settings.set("auto_catchup_enabled", "1")

    person = Person(first_name="Max", last_name="Mustermann", email="max@example.com",
                    is_active=True)
    db.add(person)
    db.flush()
    for name, cents in [("Spezi", 200), ("Kinder Country", 80), ("Wasser", 100),
                        ("Kaffee", 150)]:
        db.add(Product(name=name, price_cents=cents, is_active=True))
    db.commit()
    return {"person_id": person.id, "settings": settings}


@pytest.fixture()
def book(db):
    """Create a consumption row for the configured person."""
    from app.models import Consumption, ConsumptionStatus

    def _book(person_id, name, cents, quantity=1, at=None, product_id=None):
        total = cents * quantity
        row = Consumption(
            person_id=person_id,
            product_id=product_id,
            product_name=name,
            unit_price_cents=cents,
            quantity=quantity,
            total_cents=total,
            currency="EUR",
            status=ConsumptionStatus.OFFEN,
            created_at=at or BOOKING_TIME,
        )
        db.add(row)
        db.commit()
        return row

    return _book


@pytest.fixture()
def bill(app, db, configured, mailer):
    """Run the billing engine for the configured person."""
    from app.services.billing import run_daily_billing
    from app.settings_service import Settings

    def _bill(period=BASE_DATE, trigger="automatic", use_mailer=None, persons=None):
        return run_daily_billing(
            db,
            Settings(db),
            period,
            trigger=trigger,
            person_ids=persons if persons is not None else [configured["person_id"]],
            mailer=use_mailer or mailer,
            retry_attempts=1,
            retry_delay=0.0,
        )

    return _bill


@pytest.fixture()
def balance(db, configured):
    from app.services.billing import open_balance_cents

    def _balance(person_id=None):
        return open_balance_cents(db, person_id or configured["person_id"])

    return _balance


@pytest.fixture()
def invoices(db):
    from app.models import Invoice

    def _invoices():
        return list(db.query(Invoice).order_by(Invoice.id).all())

    return _invoices


@pytest.fixture()
def csrf(client):
    def _token(path="/login"):
        html = client.get(path).get_data(as_text=True)
        m = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', html)
        assert m, f"kein CSRF-Token auf {path}"
        return m.group(1)

    return _token


@pytest.fixture()
def admin(app, db, client):
    """Complete the setup with one active admin and log ``client`` in."""
    from app.models import AdminUser, SetupState
    from app.security import hash_password
    from app.settings_service import SETUP_COMPLETED, Settings

    settings = Settings(db)
    settings.set(SETUP_COMPLETED, "true")
    state = db.get(SetupState, 1)
    state.completed = True
    state.current_step = 6
    db.add(AdminUser(username="testadmin", email="admin@example.com",
                     password_hash=hash_password("Sicher!2026x"), is_active=True))
    db.commit()

    token = re.search(
        r'name="csrf_token"[^>]*value="([^"]+)"',
        client.get("/login").get_data(as_text=True),
    ).group(1)
    client.post("/login", data={"identifier": "testadmin", "password": "Sicher!2026x",
                                "csrf_token": token})
    return client


@pytest.fixture()
def smtp(app, db, admin):
    """A completed, mail-ready setup - SMTP points at a black hole host."""
    from app.settings_service import Settings

    settings = Settings(db)
    settings.set("paypal_me_username", "JONASNOEK1")
    settings.set("paypal_base_url", "https://www.paypal.me")
    settings.set("currency", "EUR")
    settings.set("smtp_host", "smtp.invalid")
    settings.set("smtp_port", "587")
    settings.set("smtp_encryption", "starttls")
    settings.set("mail_from_address", "absender@example.com")
    settings.set("mail_from_name", "Verzehrabrechnung")
    settings.set("auto_billing_enabled", "1")
    settings.set("auto_billing_time", "17:00")
    settings.set("auto_catchup_enabled", "1")
    db.commit()
    return admin
