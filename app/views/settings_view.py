"""Settings pages: PayPal, billing, e-mail templates, SMTP, system."""

from __future__ import annotations

import logging
from datetime import datetime

from flask import Blueprint, current_app, flash, g, redirect, render_template, request, url_for
from sqlalchemy import select

from ..mailer import Mailer
from ..models import Person
from ..money import SUPPORTED_CURRENCIES
from ..paypal import DEFAULT_BASE_URL, build_link
from ..secrets_store import get_store
from ..security import audit, login_required, setup_required, validate_csrf
from ..services.billing import get_tz, local_now
from ..services.mail_factory import is_email_configured, smtp_config_from_settings
from ..settings_service import (
    APP_NAME,
    APP_URL,
    AUTO_BILLING_ENABLED,
    AUTO_BILLING_TIME,
    CURRENCY,
    DATE_FORMAT,
    EMAIL_BODY_HTML_TEMPLATE,
    EMAIL_BODY_TEXT_TEMPLATE,
    EMAIL_SUBJECT_TEMPLATE,
    INVOICE_NUMBER_PREFIX,
    MAIL_FROM_ADDRESS,
    MAIL_FROM_NAME,
    MAIL_REPLY_TO,
    PAYPAL_BASE_URL,
    PAYPAL_ME_USERNAME,
    SMTP_ENCRYPTION,
    SMTP_HOST,
    SMTP_PORT,
    SMTP_USERNAME,
    TIMEZONE,
    Settings,
)
from ..validators import EMAIL_RE, Validator

log = logging.getLogger(__name__)

bp = Blueprint("settings", __name__, url_prefix="/einstellungen")

TABS = [
    ("paypal", "PayPal"),
    ("billing", "Abrechnung"),
    ("email", "E-Mail & Vorlagen"),
    ("smtp", "SMTP"),
    ("system", "System"),
]


@bp.route("/")
@setup_required
@login_required
def index():
    return redirect(url_for("settings.general", tab="paypal"))


def _available_timezones() -> list[str]:
    common = [
        "Europe/Berlin", "Europe/Vienna", "Europe/Zurich", "Europe/Amsterdam",
        "Europe/Paris", "Europe/Madrid", "Europe/Rome", "Europe/Prague",
        "Europe/Warsaw", "Europe/Lisbon", "Europe/London", "Europe/Moscow",
        "America/New_York", "America/Chicago", "America/Denver",
        "America/Los_Angeles", "America/Sao_Paulo", "UTC",
    ]
    return common


def _sample_preview(db, settings: Settings) -> dict:
    person = db.execute(select(Person).order_by(Person.id)).scalars().first()
    if person is None:
        person = Person(id=0, first_name="Max", last_name="Mustermann",
                        email="max@example.com")
    items = [
        {"product_name": "Spezi", "unit_price_cents": 200, "quantity": 1, "total_cents": 200},
        {"product_name": "Kinder Country", "unit_price_cents": 80, "quantity": 3, "total_cents": 240},
        {"product_name": "Kaffee", "unit_price_cents": 150, "quantity": 1, "total_cents": 150},
    ]
    total = sum(i["total_cents"] for i in items)
    username = settings.paypal_username or "DEINPAYPALNAME"
    try:
        link = build_link(username, total, settings.currency, settings.paypal_base_url)
    except Exception:  # noqa: BLE001
        link = f"{settings.paypal_base_url}/{username}/5.90{settings.currency}"
    from .. import email_templates as et

    rendered = et.render(
        person=person,
        items=items,
        total_cents=total,
        currency=settings.currency,
        paypal_link=link,
        paypal_username=username,
        invoice_number="RE-20260101-0001",
        period_date=datetime.now().date(),
        app_name=settings.app_name,
        subject_template=settings.get(EMAIL_SUBJECT_TEMPLATE) or "",
        text_template=settings.get(EMAIL_BODY_TEXT_TEMPLATE) or "",
        html_template=settings.get(EMAIL_BODY_HTML_TEMPLATE) or "",
        date_format=settings.date_format,
    )
    from .. import email_templates as et2

    return {
        "subject": rendered.subject,
        "text": rendered.text,
        "html": et2.wrap_html_document(
            body=rendered.html,
            subject=rendered.subject,
            app_name=settings.app_name,
            invoice_number="RE-20260101-0001",
            period=datetime.now().strftime(settings.date_format),
        ),
        "missing": rendered.missing,
        "link": link,
        "total": total,
    }


@bp.route("/<tab>", methods=["GET", "POST"])
@setup_required
@login_required
def general(tab: str):
    if tab not in {t[0] for t in TABS}:
        flash("Unbekannter Einstellungsbereich.", "error")
        return redirect(url_for("settings.general", tab="paypal"))

    db = g.db
    settings = Settings(db)
    store = get_store()
    errors: dict[str, str] = {}

    if request.method == "POST":
        validate_csrf()
        errors = _handle_save(tab, request.form, settings, store)
        if not errors:
            audit(db, f"settings.{tab}", detail="Aenderungen gespeichert")
            flash("Einstellungen gespeichert.", "success")
            from ..scheduler import reschedule

            try:
                reschedule(current_app._get_current_object())
            except Exception:  # noqa: BLE001
                pass
            return redirect(url_for("settings.general", tab=tab))
        flash(next(iter(errors.values())), "error")

    preview = _sample_preview(db, settings) if tab in {"email", "paypal"} else None
    # Maskierte Vorschau: reicht, um das gespeicherte Passwort wiederzuerkennen,
    # ohne es offenzulegen. Fuer Gmail-App-Passwoerter sind die letzten vier
    # Zeichen in aller Regel eindeutig.
    current_pw = (store.get("SMTP_PASSWORD", "") or "").strip()
    if current_pw:
        smtp_password_hint = "*" * 8 + (current_pw[-4:] if len(current_pw) > 4 else current_pw)
    else:
        smtp_password_hint = ""
    context = {
        "tab": tab,
        "tabs": TABS,
        "settings": settings,
        "errors": errors,
        "form": request.form if request.method == "POST" else {},
        "currencies": SUPPORTED_CURRENCIES,
        "timezones": _available_timezones(),
        "preview": preview,
        "smtp_password_set": store.has("SMTP_PASSWORD"),
        "smtp_password_from_env": store.is_overridden_by_env("SMTP_PASSWORD"),
        "smtp_password_hint": smtp_password_hint,
        "secrets_writable": store.writable(),
        "email_configured": is_email_configured(settings),
        "now": local_now(get_tz(settings.timezone)),
        "default_paypal_base_url": DEFAULT_BASE_URL,
    }
    return render_template("settings/general.html", **context)


# ---------------------------------------------------------------------------
# save handlers
# ---------------------------------------------------------------------------
def _handle_save(tab: str, form, settings: Settings, store) -> dict[str, str]:
    if tab == "paypal":
        v = Validator()
        username = v.paypal_username("paypal_me_username", form.get("paypal_me_username"))
        currency = v.currency_code("currency", form.get("currency"))
        base_url = v.required_text("paypal_base_url", form.get("paypal_base_url"), label="Basis-URL", max_len=200)
        if base_url and not base_url.startswith("https://"):
            v.add("paypal_base_url", "Die Basis-URL muss mit https:// beginnen")
        if v.errors:
            return v.errors
        settings.set_many(
            {
                PAYPAL_ME_USERNAME: username,
                CURRENCY: currency,
                PAYPAL_BASE_URL: base_url.rstrip("/"),
            }
        )
        g.db.commit()
        return {}

    if tab == "billing":
        v = Validator()
        enabled = v.boolean(AUTO_BILLING_ENABLED, form.get(AUTO_BILLING_ENABLED))
        time_value = v.time_of_day(AUTO_BILLING_TIME, form.get(AUTO_BILLING_TIME))
        timezone_name = v.timezone(TIMEZONE, form.get(TIMEZONE))
        catchup = v.boolean("auto_catchup_enabled", form.get("auto_catchup_enabled"))
        prefix = v.optional_text(
            INVOICE_NUMBER_PREFIX, form.get(INVOICE_NUMBER_PREFIX), max_len=20
        ) or "RE-"
        if not prefix.strip():
            v.add(INVOICE_NUMBER_PREFIX, "Praefix darf nicht leer sein")
        if v.errors:
            return v.errors
        settings.set_many(
            {
                AUTO_BILLING_ENABLED: "true" if enabled else "false",
                AUTO_BILLING_TIME: time_value,
                TIMEZONE: timezone_name,
                "auto_catchup_enabled": "true" if catchup else "false",
                INVOICE_NUMBER_PREFIX: prefix,
            }
        )
        g.db.commit()
        return {}

    if tab == "smtp":
        v = Validator()
        host = v.required_text(SMTP_HOST, form.get(SMTP_HOST), label="SMTP-Server", max_len=200)
        try:
            port = int((form.get(SMTP_PORT) or "587").strip())
            if not (1 <= port <= 65535):
                raise ValueError
        except ValueError:
            v.add(SMTP_PORT, "Ungueltiger SMTP-Port (1-65535)")
            port = 587
        encryption = v.choice(SMTP_ENCRYPTION, form.get(SMTP_ENCRYPTION), ["none", "starttls", "ssl"], default="starttls")
        username = v.optional_text(SMTP_USERNAME, form.get(SMTP_USERNAME), max_len=200) or ""
        from_name = v.required_text(MAIL_FROM_NAME, form.get(MAIL_FROM_NAME), label="Absendername", max_len=120)
        from_address = v.email(MAIL_FROM_ADDRESS, form.get(MAIL_FROM_ADDRESS))
        reply_to = ""
        if form.get(MAIL_REPLY_TO):
            reply_to = v.email(MAIL_REPLY_TO, form.get(MAIL_REPLY_TO), required=False)
        password = form.get("smtp_password") or ""
        clear_password = v.boolean("clear_password", form.get("clear_password"))
        if password and not store.writable():
            v.add("smtp_password", "Speicher fuer Secrets nicht beschreibbar. Bitte SMTP_PASSWORD als Umgebungsvariable setzen.")
        if v.errors:
            return v.errors
        if password:
            store.set("SMTP_PASSWORD", password)
        elif clear_password:
            store.set("SMTP_PASSWORD", None)
        settings.set_many(
            {
                SMTP_HOST: host,
                SMTP_PORT: port,
                SMTP_ENCRYPTION: encryption,
                SMTP_USERNAME: username,
                MAIL_FROM_NAME: from_name,
                MAIL_FROM_ADDRESS: from_address,
                MAIL_REPLY_TO: reply_to,
            }
        )
        g.db.commit()
        return {}

    if tab == "email":
        v = Validator()
        subject = v.required_text(EMAIL_SUBJECT_TEMPLATE, form.get(EMAIL_SUBJECT_TEMPLATE), label="Betreff", max_len=300)
        text_body = v.required_text(EMAIL_BODY_TEXT_TEMPLATE, form.get(EMAIL_BODY_TEXT_TEMPLATE), label="Text-Vorlage", max_len=20000)
        html_body = v.required_text(EMAIL_BODY_HTML_TEMPLATE, form.get(EMAIL_BODY_HTML_TEMPLATE), label="HTML-Vorlage", max_len=40000)
        for key, label in (
            ("{produkte}", "{produkte} wird durch die Text-Tabelle ersetzt"),
            ("{produkte_html}", "{produkte_html} wird durch die HTML-Tabelle ersetzt"),
        ):
            if key not in text_body and key not in html_body:
                v.add(EMAIL_BODY_TEXT_TEMPLATE, f"Hinweis: {label}")
        if v.errors:
            return {k: v.errors[k] for k in list(v.errors)[:1]}
        settings.set_many(
            {
                EMAIL_SUBJECT_TEMPLATE: subject,
                EMAIL_BODY_TEXT_TEMPLATE: text_body,
                EMAIL_BODY_HTML_TEMPLATE: html_body,
            }
        )
        g.db.commit()
        return {}

    if tab == "system":
        v = Validator()
        app_name = v.required_text(APP_NAME, form.get(APP_NAME), label="Anwendungsname", max_len=80)
        app_url = v.optional_text(APP_URL, form.get(APP_URL), max_len=200) or ""
        if app_url and not app_url.startswith(("http://", "https://")):
            v.add(APP_URL, "URL muss mit http:// oder https:// beginnen")
        date_format = v.required_text(DATE_FORMAT, form.get(DATE_FORMAT), label="Datumsformat", max_len=40)
        try:
            datetime.now().strftime(date_format)
        except (ValueError, TypeError):
            v.add(DATE_FORMAT, "Ungueltiges Datumsformat (Python-Strftime-Syntax, z. B. %d.%m.%Y)")
        if v.errors:
            return v.errors
        settings.set_many(
            {
                APP_NAME: app_name,
                APP_URL: app_url.rstrip("/"),
                DATE_FORMAT: date_format,
            }
        )
        g.db.commit()
        return {}

    return {"tab": "Unbekannter Bereich"}


# ---------------------------------------------------------------------------
# actions
# ---------------------------------------------------------------------------
@bp.route("/smtp/password-reveal", methods=["POST"])
@setup_required
@login_required
def reveal_smtp_password():
    """Show the stored SMTP password on request.

    Deliberately a POST with a CSRF token: the secret must not end up in the
    URL, in the browser history or in a prefetch. The value is also written
    to the audit log, without the secret itself, so that reading it stays
    traceable.

    The password lives in ``instance/secrets.env`` on the same host, so an
    administrator able to use this form can read the file directly as root
    anyway. Hiding it here would not protect anything - it would only make
    it hard to tell *which* password is currently active.
    """
    validate_csrf()
    db = g.db
    store = get_store()

    if not store.has("SMTP_PASSWORD"):
        flash("Es ist kein SMTP-Passwort hinterlegt.", "error")
        return redirect(url_for("settings.general", tab="smtp"))

    password = store.get("SMTP_PASSWORD", "") or ""
    audit(
        db,
        "smtp.password_reveal",
        detail=(
            f"Passwort im Klartext angezeigt, Quelle: "
            f"{'Umgebungsvariable' if store.is_overridden_by_env('SMTP_PASSWORD') else 'Secrets-Datei'}"
        ),
    )
    return render_template(
        "settings/smtp_password.html",
        password=password,
        from_env=store.is_overridden_by_env("SMTP_PASSWORD"),
    )


@bp.route("/test-mail", methods=["POST"])
@setup_required
@login_required
def test_email():
    validate_csrf()
    db = g.db
    settings = Settings(db)
    store = get_store()
    recipient = (request.form.get("recipient") or settings.get(MAIL_FROM_ADDRESS) or "").strip()
    if not recipient or not EMAIL_RE.match(recipient.lower()):
        flash("Bitte eine gueltige Empfaengeradresse angeben.", "error")
        return redirect(url_for("settings.general", tab="smtp"))

    mailer = Mailer(smtp_config_from_settings(settings, store))
    ok, message = mailer.test_connection()
    if not ok:
        flash(f"SMTP-Verbindung fehlgeschlagen: {message}", "error")
        audit(db, "smtp.test", detail=message)
        return redirect(url_for("settings.general", tab="smtp"))


    total = 590
    try:
        link = build_link(settings.paypal_username, total, settings.currency, settings.paypal_base_url)
    except Exception:  # noqa: BLE001
        link = "(PayPal-Benutzername nicht konfiguriert)"
    from .. import email_templates as et

    person = db.execute(select(Person).order_by(Person.id)).scalars().first()
    if person is None:
        person = Person(id=0, first_name="Max", last_name="Mustermann", email=recipient)
    rendered = et.render(
        person=person,
        items=[
            {"product_name": "Spezi", "unit_price_cents": 200, "quantity": 1, "total_cents": 200},
            {"product_name": "Kinder Country", "unit_price_cents": 80, "quantity": 3, "total_cents": 240},
            {"product_name": "Kaffee", "unit_price_cents": 150, "quantity": 1, "total_cents": 150},
        ],
        total_cents=total,
        currency=settings.currency,
        paypal_link=link,
        paypal_username=settings.paypal_username,
        invoice_number="TEST-0001",
        period_date=datetime.now().date(),
        app_name=settings.app_name,
        subject_template=settings.get(EMAIL_SUBJECT_TEMPLATE) or "",
        text_template=settings.get(EMAIL_BODY_TEXT_TEMPLATE) or "",
        html_template=settings.get(EMAIL_BODY_HTML_TEMPLATE) or "",
        date_format=settings.date_format,
    )
    html_doc = et.wrap_html_document(
        body=rendered.html,
        subject=f"{settings.app_name}: Test",
        app_name=settings.app_name,
        invoice_number="TEST-0001",
        period=datetime.now().strftime(settings.date_format),
    )
    result = mailer.send(
        to=recipient,
        subject=f"{settings.app_name}: Test-E-Mail",
        text_body=f"{rendered.text}\n\n---\nDies ist eine Test-E-Mail. Es wurde nichts abgebucht.",
        html_body=html_doc,
    )
    if result.ok:
        flash(f"Test-E-Mail wurde an {recipient} gesendet.", "success")
    else:
        flash(f"Test-E-Mail fehlgeschlagen: {result.error}", "error")
    audit(db, "smtp.test_mail", target=recipient, detail=result.error or "erfolgreich")
    return redirect(url_for("settings.general", tab="smtp"))


@bp.route("/paypal/test-link", methods=["POST"])
@setup_required
@login_required
def test_paypal_link():
    validate_csrf()
    db = g.db
    settings = Settings(db)
    v = Validator()
    cents = v.try_money("test_amount", request.form.get("test_amount"), currency=settings.currency)
    username = v.paypal_username(
        "paypal_me_username", request.form.get("paypal_me_username") or settings.paypal_username
    )
    if v.errors:
        flash(v.first_error(), "error")
        return redirect(url_for("settings.general", tab="paypal"))
    try:
        link = build_link(username, cents or 0, settings.currency, settings.paypal_base_url)
    except Exception as exc:  # noqa: BLE001
        flash(f"Link konnte nicht erzeugt werden: {exc}", "error")
        return redirect(url_for("settings.general", tab="paypal"))
    flash(f"Test-Link: {link}", "success", )
    audit(db, "paypal.test_link", detail=link)
    return redirect(url_for("settings.general", tab="paypal"))
