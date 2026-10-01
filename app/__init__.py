"""Application factory."""

from __future__ import annotations

import logging
import os
from datetime import timedelta

from flask import Flask, g, render_template, request, session
from werkzeug.middleware.proxy_fix import ProxyFix

from . import migrations
from .config import INSTANCE_DIR, MIGRATIONS_DIR, Config, configure_logging
from .db import init_engine, remove_session
from .models import AppMeta, SetupState
from .scheduler import init_scheduler
from .secrets_store import init_store
from .settings_service import DEFAULTS, Settings
from .views import register_blueprints

log = logging.getLogger(__name__)

__version__ = "1.0.0"


def create_app(
    config: Config | None = None,
    *,
    run_migrations: bool = True,
    start_scheduler: bool | None = None,
) -> Flask:
    cfg = config or Config()
    configure_logging(cfg)

    app = Flask(
        __name__,
        instance_path=str(INSTANCE_DIR),
        instance_relative_config=False,
    )

    secrets = init_store(cfg.SECRETS_FILE)
    secret_key = cfg.SECRET_KEY or secrets.get("SECRET_KEY") or ""
    if not secret_key:
        secret_key = secrets.ensure_secret_key()
        log.warning(
            "Kein SECRET_KEY gesetzt - es wurde einer in %s erzeugt. "
            "Fuer Produktivbetrieb bitte SECRET_KEY in der .env setzen.",
            cfg.SECRETS_FILE,
        )

    app.config.update(
        SECRET_KEY=secret_key,
        SESSION_COOKIE_NAME="pps_session",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=cfg.APP_ENV == "production" and _looks_https(),
        PERMANENT_SESSION_LIFETIME=timedelta(hours=4),
        MAX_CONTENT_LENGTH=16 * 1024 * 1024,
        JSON_SORT_KEYS=False,
        APP_VERSION=__version__,
        TRUST_PROXY=bool(os.environ.get("TRUST_PROXY", "").strip()),
        WTF_CSRF_ENABLED=True,  # custom implementation, kept for clarity
        SESSION_COOKIE_PARTITIONED=False,
    )
    if cfg.TESTING:
        app.config.update(
            SESSION_COOKIE_SECURE=False,
            TESTING=True,
            WTF_CSRF_ENABLED=False,
        )

    if app.config["TRUST_PROXY"]:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    init_engine(cfg.DATABASE_URL)

    if run_migrations:
        applied = migrations.run_migrations(MIGRATIONS_DIR)
        if applied:
            log.info("Migrationen ausgefuehrt: %s", ", ".join(applied))
    _ensure_seed_rows()
    if run_migrations:
        _backfill_accounts()

    app.extensions["settings"] = cfg
    app.extensions["db_session"] = None
    app.extensions["migrations_dir"] = MIGRATIONS_DIR

    _register_request_hooks(app)
    _register_template_globals(app)
    _register_error_handlers(app)
    register_blueprints(app)

    should_start = cfg.SCHEDULER_ENABLED if start_scheduler is None else start_scheduler
    if should_start:
        init_scheduler(app)
    else:
        log.info("Scheduler nicht gestartet (SCHEDULER_ENABLED=%s)", should_start)

    return app


def _looks_https() -> bool:
    url = (os.environ.get("APP_URL") or "").lower()
    return url.startswith("https://")


def _ensure_seed_rows() -> None:
    """Create singleton rows and seed default settings after the first migration."""
    from sqlalchemy import select

    from .db import session_scope
    from .models import Setting

    with session_scope() as session:
        if session.get(SetupState, 1) is None:
            session.add(SetupState(id=1, completed=False, current_step=1))
        meta = session.get(AppMeta, "schema_version")
        if meta is None:
            session.add(
                AppMeta(key="schema_version", value=str(migrations.current_version()))
            )
        else:
            meta.value = str(migrations.current_version())

        existing = set(session.execute(select(Setting.key)).scalars().all())
        settings = Settings(session)
        for key, value in DEFAULTS.items():
            if key not in existing:
                settings.set(key, value)


def _backfill_accounts() -> None:
    """Carry records that predate migration 003 into the credit accounts.

    Runs after the schema exists and does nothing once the marker is set, so it
    costs a single indexed read per start. A failure must never keep the app from
    starting: accounts can be repaired by hand, a boot loop cannot.
    """
    from .db import session_scope
    from .services.accounts import ensure_accounts

    try:
        with session_scope() as session:
            stats = ensure_accounts(session)
        if stats["accounts_created"] or stats["entries_created"]:
            log.info(
                "Guthabenkonten uebernommen: %s Konten, %s Bewegungen",
                stats["accounts_created"],
                stats["entries_created"],
            )
    except Exception:  # pragma: no cover - defensive, start must survive
        log.exception("Rueckuebernahme der Guthabenkonten fehlgeschlagen")


def _register_request_hooks(app: Flask) -> None:
    from .db import get_session
    from .security import current_admin, csrf_token

    @app.before_request
    def _open_session() -> None:
        g.db = get_session()
        app.extensions["db_session"] = g.db
        try:
            g.admin = current_admin(g.db) if session.get("admin_id") else None
        except Exception:  # noqa: BLE001 - never block rendering
            g.db.rollback()
            g.admin = None

    @app.teardown_request
    def _close_session(exc=None) -> None:
        db = g.pop("db", None)
        if db is not None:
            if exc is not None:
                db.rollback()
            remove_session()

    @app.context_processor
    def _inject() -> dict:
        db = getattr(g, "db", None)
        context: dict = {"csrf_token": csrf_token, "app_version": __version__}
        if db is not None:
            try:
                settings = Settings(db)
                context["settings_obj"] = settings
                context["app_name"] = settings.app_name
                context["currency"] = settings.currency
                context["current_admin"] = getattr(g, "admin", None)
                context["timezone"] = settings.timezone
            except Exception:  # noqa: BLE001
                context["app_name"] = "Verzehrabrechnung"
                context["currency"] = "EUR"
                context["current_admin"] = None
                context["timezone"] = "Europe/Berlin"
        else:
            context["app_name"] = "Verzehrabrechnung"
            context["currency"] = "EUR"
            context["current_admin"] = None
            context["timezone"] = "Europe/Berlin"
        return context


def _register_template_globals(app: Flask) -> None:
    from .money import format_cents, format_decimal_input
    from .models import ConsumptionStatus, InvoiceStatus, PaymentStatus
    from .security import csrf_token

    @app.template_filter("money")
    def _money(value, currency: str = "EUR") -> str:
        try:
            return format_cents(int(value or 0), currency or "EUR")
        except (TypeError, ValueError):
            return "0,00 \u20ac"

    @app.template_filter("money_plain")
    def _money_plain(value) -> str:
        try:
            return format_cents(int(value or 0), "EUR", symbol=False)
        except (TypeError, ValueError):
            return "0,00"

    @app.template_filter("dt")
    def _dt(value, fmt: str = "%d.%m.%Y %H:%M"):
        if value is None:
            return "-"
        try:
            return value.strftime(fmt)
        except (AttributeError, ValueError):
            return "-"

    @app.template_filter("localdt")
    def _localdt(value, fmt: str = "%d.%m.%Y %H:%M", tz_name: str = "Europe/Berlin"):
        from datetime import timezone

        from .services.billing import get_tz

        if value is None:
            return "-"
        try:
            return (
                value.replace(tzinfo=timezone.utc)
                .astimezone(get_tz(tz_name))
                .strftime(fmt)
            )
        except (AttributeError, ValueError):
            return "-"

    @app.template_filter("status_badge")
    def _status_badge(status) -> str:
        from markupsafe import Markup, escape

        value = getattr(status, "value", status)
        if not value:
            return ""
        key = str(value).lower().replace(" ", "-")
        return Markup(
            f'<span class="badge badge-{escape(key)}">{escape(str(value))}</span>'
        )

    app.jinja_env.filters["money"] = _money
    app.jinja_env.filters["money_plain"] = _money_plain
    app.jinja_env.filters["dt"] = _dt
    app.jinja_env.filters["localdt"] = _localdt
    app.jinja_env.globals.update(
        ConsumptionStatus=ConsumptionStatus,
        InvoiceStatus=InvoiceStatus,
        PaymentStatus=PaymentStatus,
        format_decimal_input=format_decimal_input,
        csrf_token=csrf_token,
    )


def _register_error_handlers(app: Flask) -> None:
    from flask import jsonify

    def _wants_json() -> bool:
        return request.path.startswith("/api/") or request.accept_mimetypes.best == "application/json"

    @app.errorhandler(400)
    def _bad_request(error):
        if _wants_json():
            return jsonify({"error": getattr(error, "description", "Ungueltige Anfrage")}), 400
        return render_template("errors/error.html", code=400, title="Ungueltige Anfrage",
                               message=getattr(error, "description", "Die Anfrage war ungueltig.")), 400

    @app.errorhandler(403)
    def _forbidden(error):
        if _wants_json():
            return jsonify({"error": "Zugriff verweigert"}), 403
        return render_template("errors/error.html", code=403, title="Zugriff verweigert",
                               message="Du hast keine Berechtigung fuer diese Seite."), 403

    @app.errorhandler(404)
    def _not_found(error):
        if _wants_json():
            return jsonify({"error": "Nicht gefunden"}), 404
        return render_template("errors/error.html", code=404, title="Seite nicht gefunden",
                               message="Die angeforderte Seite existiert nicht."), 404

    @app.errorhandler(413)
    def _too_large(error):
        return render_template("errors/error.html", code=413, title="Zu gross",
                               message="Der Upload ist zu gross."), 413

    @app.errorhandler(429)
    def _too_many(error):
        return render_template("errors/error.html", code=429, title="Zu viele Versuche",
                               message="Bitte warte einen Moment und versuche es erneut."), 429

    @app.errorhandler(500)
    def _server_error(error):
        log.exception("Interner Serverfehler: %s", request.path)
        if app.config.get("TESTING") or app.config.get("DEBUG"):
            raise error
        if _wants_json():
            return jsonify({"error": "Interner Serverfehler"}), 500
        return render_template("errors/error.html", code=500, title="Interner Fehler",
                               message="Es ist ein unerwarteter Fehler aufgetreten."), 500

    @app.errorhandler(Exception)
    def _unhandled(error):
        from werkzeug.exceptions import HTTPException

        if isinstance(error, HTTPException):
            return error
        log.exception("Unbehandelte Ausnahme: %s", request.path)
        if app.config.get("TESTING") or app.config.get("DEBUG"):
            raise error
        return render_template("errors/error.html", code=500, title="Interner Fehler",
                               message="Es ist ein unerwarteter Fehler aufgetreten."), 500


__all__ = ["create_app", "Config", "__version__"]
