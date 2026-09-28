"""First-run setup wizard (6 steps).

Reachable only while the setup is not completed. Afterwards ``/setup``
redirects to the login page.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from sqlalchemy import func, or_, select

from ..db import get_engine, get_session, remove_session
from ..mailer import Mailer
from ..models import AdminUser, SetupState, utcnow
from ..secrets_store import get_store
from ..security import (
    SESSION_KEY_WIZARD,
    audit,
    hash_password,
    init_session,
    password_problems,
    validate_csrf,
)
from ..services.mail_factory import smtp_config_from_settings
from ..settings_service import (
    AUTO_BILLING_ENABLED,
    AUTO_BILLING_TIME,
    CURRENCY,
    MAIL_FROM_ADDRESS,
    MAIL_FROM_NAME,
    PAYPAL_ME_USERNAME,
    SETUP_COMPLETED,
    SMTP_ENCRYPTION,
    SMTP_HOST,
    SMTP_PORT,
    SMTP_USERNAME,
    TIMEZONE,
    Settings,
)
from ..validators import EMAIL_RE, Validator

log = logging.getLogger(__name__)

bp = Blueprint("setup", __name__, url_prefix="/setup")

STEPS = [
    (1, "Datenbank", "Datenbank pruefen und initialisieren"),
    (2, "Administrator", "Administrator-Konto anlegen"),
    (3, "PayPal", "PayPal.Me konfigurieren"),
    (4, "E-Mail", "SMTP-Mailserver konfigurieren"),
    (5, "Abrechnung", "Automatische Tagesabrechnung einstellen"),
    (6, "Fertig", "Einrichtung abschliessen"),
]
TOTAL_STEPS = len(STEPS)


def _state() -> SetupState:
    state = g.db.get(SetupState, 1)
    if state is None:
        state = SetupState(id=1, completed=False, current_step=1)
        g.db.add(state)
        g.db.flush()
    return state


def _guard():
    """Block the wizard once it is done, and keep unauthenticated users out."""
    settings = Settings(g.db)
    if settings.get_bool(SETUP_COMPLETED, False) or _state().completed:
        abort(404)
    return settings


def _wizard_data() -> dict:
    raw = session.get(SESSION_KEY_WIZARD)
    return dict(raw) if isinstance(raw, dict) else {}


def _save_wizard(data: dict) -> None:
    session[SESSION_KEY_WIZARD] = {k: v for k, v in data.items() if k != "admin_password"}


@bp.route("/", methods=["GET"])
@bp.route("", methods=["GET"])
def index():
    settings = _guard()
    state = _state()
    return redirect(url_for("setup.step", number=min(max(state.current_step, 1), TOTAL_STEPS)))


@bp.route("/<int:number>", methods=["GET", "POST"])
def step(number: int):
    settings = _guard()
    if number < 1 or number > TOTAL_STEPS:
        abort(404)

    state = _state()
    if request.method == "GET" and number > state.current_step:
        return redirect(url_for("setup.step", number=state.current_step))

    data = _wizard_data()
    error = ""
    errors: dict[str, str] = {}

    if request.method == "POST":
        validate_csrf()
        handler = {
            1: _step_database,
            2: _step_admin,
            3: _step_paypal,
            4: _step_email,
            5: _step_billing,
            6: _step_finish,
        }[number]
        outcome = handler(request.form, data, settings)
        error = outcome.get("error", "")
        errors = outcome.get("errors", {})
        data = outcome.get("data", data)
        if outcome.get("done"):
            return redirect(outcome.get("redirect", url_for("auth.login")))
        if error or errors:
            # keep the user on this step and show why - never advance on a failure
            return render_step(number, settings, data, error, errors)
        _save_wizard(data)
        state = _state()
        state.current_step = max(state.current_step, number + 1)
        g.db.commit()
        return redirect(url_for("setup.step", number=number + 1))

    return render_step(number, settings, data, error, errors)


def render_step(number: int, settings: Settings, data: dict, error: str,
                errors: dict[str, str]):
    """Render one wizard step in place, e.g. to show a validation error."""
    context = {
        "settings": settings,
        "data": data,
        "steps": STEPS,
        "step_number": number,
        "total_steps": TOTAL_STEPS,
        "error": error,
        "errors": errors,
        "title": STEPS[number - 1][1],
        "subtitle": STEPS[number - 1][2],
    }
    if number in (1, 6):
        context["db_info"] = _database_info()
    if number == 4:
        context["has_smtp_password"] = get_store().has("SMTP_PASSWORD")
    if number == 6:
        context["admin_count"] = int(
            g.db.execute(select(func.count(AdminUser.id))).scalar_one() or 0
        )
    return render_template(f"setup/step{number}.html", **context)


def _step_admin(form, data: dict, settings: Settings) -> dict:
    v = Validator()
    username = v.username("admin_username", form.get("admin_username") or data.get("admin_username"))
    email = v.email("admin_email", form.get("admin_email") or data.get("admin_email"))
    display = v.optional_text("display_name", form.get("display_name") or data.get("display_name"), max_len=120)
    password = form.get("admin_password") or ""
    confirm = form.get("admin_password_confirm") or ""

    if not password:
        v.add("admin_password", "Passwort ist erforderlich")
    elif password != confirm:
        v.add("admin_password_confirm", "Passwoerter stimmen nicht ueberein")
    else:
        for problem in password_problems(password, username, email):
            v.add("admin_password", problem)

    if v.errors:
        return {
            "error": v.first_error(),
            "errors": v.errors,
            "data": {**data, "admin_username": username, "admin_email": email, "display_name": display},
        }

    existing = g.db.execute(
        select(AdminUser).where(
            or_(
                func.lower(AdminUser.username) == username.lower(),
                func.lower(AdminUser.email) == email.lower(),
            )
        )
    ).scalars().first()
    if existing is not None:
        return {
            "error": "Benutzername oder E-Mail-Adresse wird bereits verwendet.",
            "errors": {"admin_username": "Bereits vergeben"},
            "data": {**data, "admin_username": username, "admin_email": email, "display_name": display},
        }

    admin = AdminUser(
        username=username,
        email=email,
        display_name=display,
        password_hash=hash_password(password),
        is_active=True,
    )
    g.db.add(admin)
    g.db.commit()
    data = {**data, "admin_username": username, "admin_email": email, "display_name": display}
    audit(g.db, "setup.admin_created", target=username, actor=username)
    init_session(admin)
    return {"data": data, "redirect": url_for("setup.step", number=3)}


def _step_paypal(form, data: dict, settings: Settings) -> dict:
    v = Validator()
    username = v.paypal_username("paypal_me_username", form.get("paypal_me_username") or settings.paypal_username or data.get("paypal_me_username"))
    currency = v.currency_code("currency", form.get("currency") or settings.currency)
    base_url = v.required_text("paypal_base_url", form.get("paypal_base_url") or "https://www.paypal.me", label="PayPal-Basis-URL", max_len=200)
    if base_url and not base_url.startswith("https://"):
        v.add("paypal_base_url", "Die Basis-URL muss mit https:// beginnen")
    if v.errors:
        return {"error": v.first_error(), "errors": v.errors, "data": data}

    settings.set_many(
        {
            PAYPAL_ME_USERNAME: username,
            CURRENCY: currency,
            "paypal_base_url": base_url.rstrip("/"),
        }
    )
    g.db.commit()
    return {"data": {**data, "paypal_me_username": username}, "redirect": url_for("setup.step", number=4)}


def _step_email(form, data: dict, settings: Settings) -> dict:
    action = form.get("action") or "save"

    if action == "test":
        _apply_email_settings(form, settings, commit=True)
        recipient = (form.get("test_recipient") or settings.get(MAIL_FROM_ADDRESS) or "").strip()
        if not recipient or not EMAIL_RE.match(recipient.lower()):
            flash("Bitte eine gueltige Test-Empfaengeradresse angeben.", "error")
            return {"data": data, "redirect": url_for("setup.step", number=4)}
        mailer = Mailer(smtp_config_from_settings(settings, get_store()))
        result = mailer.send(
            to=recipient,
            subject=f"{settings.app_name}: Test-E-Mail",
            text_body=(
                "Dies ist eine Test-E-Mail deiner Verzehrabrechnung.\n\n"
                "Wenn du diese Nachricht liest, funktioniert der SMTP-Versand."
            ),
            html_body=(
                "<p>Dies ist eine <strong>Test-E-Mail</strong> deiner Verzehrabrechnung.</p>"
                "<p>Wenn du diese Nachricht liest, funktioniert der SMTP-Versand.</p>"
            ),
        )
        if result.ok:
            flash(f"Test-E-Mail wurde an {recipient} gesendet.", "success")
        else:
            flash(f"Test-E-Mail fehlgeschlagen: {result.error}", "error")
        return {"data": data, "redirect": url_for("setup.step", number=4)}

    errors = _apply_email_settings(form, settings, commit=False)
    if errors:
        return {"error": next(iter(errors.values())), "errors": errors, "data": data}
    g.db.commit()
    return {"data": data, "redirect": url_for("setup.step", number=5)}


def _apply_email_settings(form, settings: Settings, *, commit: bool) -> dict[str, str]:
    v = Validator()
    host = v.required_text("smtp_host", form.get("smtp_host"), label="SMTP-Server", max_len=200)
    port_raw = (form.get("smtp_port") or "587").strip()
    try:
        port = int(port_raw)
        if not (1 <= port <= 65535):
            raise ValueError
    except ValueError:
        v.add("smtp_port", "Ungueltiger SMTP-Port (1-65535)")
        port = 587
    encryption = v.choice("smtp_encryption", form.get("smtp_encryption"), ["none", "starttls", "ssl"], default="starttls")
    username = v.optional_text("smtp_username", form.get("smtp_username"), max_len=200) or ""
    from_name = v.required_text("mail_from_name", form.get("mail_from_name"), label="Absendername", max_len=120)
    from_address = v.email("mail_from_address", form.get("mail_from_address"))
    password = form.get("smtp_password") or ""
    if password:
        store = get_store()
        if not store.writable():
            v.add("smtp_password", "SMTP-Passwort kann nicht gespeichert werden (Schreibschutz). Bitte SMTP_PASSWORD als Umgebungsvariable setzen.")
        else:
            store.set("SMTP_PASSWORD", password)

    if v.errors:
        return v.errors

    settings.set_many(
        {
            SMTP_HOST: host,
            SMTP_PORT: port,
            SMTP_ENCRYPTION: encryption,
            SMTP_USERNAME: username,
            MAIL_FROM_NAME: from_name,
            MAIL_FROM_ADDRESS: from_address,
        }
    )
    if commit:
        g.db.commit()
    return {}


def _step_billing(form, data: dict, settings: Settings) -> dict:
    v = Validator()
    enabled = v.boolean("auto_billing_enabled", form.get("auto_billing_enabled"))
    time_value = v.time_of_day("auto_billing_time", form.get("auto_billing_time") or settings.auto_billing_time)
    timezone_name = v.timezone("timezone", form.get("timezone") or settings.timezone)
    if v.errors:
        return {"error": v.first_error(), "errors": v.errors, "data": data}

    settings.set_many(
        {
            AUTO_BILLING_ENABLED: "true" if enabled else "false",
            AUTO_BILLING_TIME: time_value,
            TIMEZONE: timezone_name,
            "auto_catchup_enabled": "true" if v.boolean("auto_catchup_enabled", form.get("auto_catchup_enabled")) else "false",
        }
    )
    g.db.commit()
    try:
        from ..scheduler import reschedule

        reschedule(current_app._get_current_object())
    except Exception:  # noqa: BLE001
        pass
    return {"data": data, "redirect": url_for("setup.step", number=6)}


def _step_finish(form, data: dict, settings: Settings) -> dict:
    state = _state()
    state.completed = True
    state.completed_at = utcnow()
    settings.set(SETUP_COMPLETED, "true")
    g.db.commit()
    session.pop(SESSION_KEY_WIZARD, None)
    store = get_store()
    if not store.has("SECRET_KEY"):
        store.ensure_secret_key()
    audit(g.db, "setup.completed", actor=getattr(g, "admin", None) and g.admin.username)
    flash("Einrichtung abgeschlossen. Du kannst dich jetzt anmelden.", "success")
    return {"done": True, "redirect": url_for("auth.login")}


def _database_info() -> dict:
    engine = get_engine()
    url = engine.url
    backend = engine.dialect.name
    info = {
        "backend": backend,
        "dialect": backend,
        "host": url.host or "lokale Datei",
        "port": url.port or "",
        "name": url.database or "",
        "ok": True,
        "error": "",
    }
    try:
        with engine.connect() as conn:
            conn.exec_driver_sql("SELECT 1")
    except Exception as exc:  # noqa: BLE001
        info["ok"] = False
        info["error"] = str(exc)
    return info


def _masked_url(url: str) -> str:
    """Hide the password in a database URL for display."""
    if "@" not in url or "//" not in url:
        return url
    scheme, rest = url.split("//", 1)
    credentials, tail = rest.rsplit("@", 1)
    user = credentials.split(":", 1)[0]
    return f"{scheme}//{user}:***@{tail}"


def _validate_db_url(raw: str) -> str:
    """Normalise and sanity-check a user supplied database URL."""
    from sqlalchemy.engine import make_url

    value = (raw or "").strip()
    if not value:
        raise ValueError("Bitte eine Verbindungs-URL angeben.")
    value = re.sub(r"^postgres(ql)?://", "postgresql://", value, flags=re.I)
    try:
        url = make_url(value)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"URL konnte nicht gelesen werden: {exc}") from exc
    if url.get_backend_name() == "sqlite":
        if not url.database or url.database == ":memory:":
            raise ValueError(
                "SQLite braucht einen Dateipfad, z. B. sqlite:////srv/app/instance/payment.db"
            )
        path = Path(url.database)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ValueError(f"Verzeichnis nicht anlegbar: {exc}") from exc
    else:
        if not url.host or not url.database:
            raise ValueError(
                "PostgreSQL braucht Host und Datenbankname, "
                "z. B. postgresql://user:pass@localhost:5432/verzehr"
            )
    return value


def _step_database(form, data: dict, settings: Settings) -> dict:
    if (form.get("action") or "") != "change":
        engine = get_engine()
        try:
            with engine.connect() as conn:
                conn.exec_driver_sql("SELECT 1")
            return {"data": {**data, "db_ok": True},
                    "redirect": url_for("setup.step", number=2)}
        except Exception as exc:  # noqa: BLE001
            return {"error": f"Datenbank nicht erreichbar: {exc}", "data": data}

    # --- switch to a different database -----------------------------------
    old_engine = get_engine()
    raw = form.get("database_url") or ""
    try:
        new_url = _validate_db_url(raw)
    except ValueError as exc:
        return {
            "error": str(exc),
            "errors": {"database_url": str(exc)},
            "data": {**data, "database_url": raw},
        }

    store = get_store()
    if getattr(current_app.extensions.get("settings"), "database_url_pinned", False):
        return {
            "error": (
                "DATABASE_URL ist als Umgebungsvariable gesetzt und kann im "
                "Assistenten nicht geaendert werden. Bitte die Variable in der "
                "Umgebung anpassen und den Dienst neu starten."
            ),
            "errors": {"database_url": "durch Umgebungsvariable gesetzt"},
            "data": {**data, "database_url": _masked_url(raw)},
        }

    from ..db import init_engine

    try:
        candidate = init_engine(new_url)
        with candidate.connect() as conn:
            conn.exec_driver_sql("SELECT 1")
    except Exception as exc:  # noqa: BLE001
        init_engine(_url_of(old_engine))     # roll back to the working engine
        return {
            "error": f"Verbindung fehlgeschlagen: {exc}",
            "errors": {"database_url": "nicht erreichbar"},
            "data": {**data, "database_url": _masked_url(raw)},
        }

    try:
        from ..config import MIGRATIONS_DIR
        from ..migrations import run_migrations

        run_migrations(MIGRATIONS_DIR)
    except Exception as exc:  # noqa: BLE001
        init_engine(_url_of(old_engine))
        return {
            "error": f"Schema konnte nicht angelegt werden: {exc}",
            "errors": {"database_url": "Migration fehlgeschlagen"},
            "data": {**data, "database_url": _masked_url(raw)},
        }

    store.set("DATABASE_URL", new_url)
    os.environ["DATABASE_URL"] = new_url
    current_app.config["DATABASE_URL"] = new_url

    db = get_session()
    state = db.get(SetupState, 1)
    if state is None:
        state = SetupState(id=1, completed=False, current_step=1)
        db.add(state)
    state.current_step = max(int(state.current_step or 1), 2)
    db.commit()
    remove_session()

    flash("Datenbankverbindung aktualisiert.", "success")
    return {"data": {**data, "database_url": _masked_url(new_url), "db_ok": True},
            "redirect": url_for("setup.step", number=2)}


def _url_of(engine) -> str:
    """Render an engine back into a URL string, password included."""
    return engine.url.render_as_string(hide_password=False)
