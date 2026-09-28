"""Admin login/logout, password change."""

from __future__ import annotations

import logging

from flask import (
    Blueprint,
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

from ..models import AdminUser, utcnow
from ..security import (
    MAX_LOGIN_ATTEMPTS,
    SESSION_KEY_WIZARD,
    SESSION_TIMEOUT_MINUTES,
    audit,
    client_ip,
    hash_password,
    init_session,
    is_rate_limited,
    login_required,
    password_problems,
    record_login_attempt,
    remaining_attempts,
    session_valid,
    validate_csrf,
    verify_password,
)
from ..settings_service import SETUP_COMPLETED, Settings

log = logging.getLogger(__name__)

bp = Blueprint("auth", __name__)


def _next_target() -> str:
    candidate = request.args.get("next") or request.form.get("next") or ""
    if candidate.startswith("/") and not candidate.startswith("//"):
        return candidate
    return url_for("main.dashboard")


@bp.route("/login", methods=["GET", "POST"])
def login():
    db = g.db
    settings = Settings(db)
    if not settings.get_bool(SETUP_COMPLETED, False):
        return redirect(url_for("setup.index"))

    if session_valid(db):
        return redirect(_next_target())

    error = ""
    identifier = ""
    if request.method == "POST":
        validate_csrf()
        identifier = (request.form.get("identifier") or "").strip()
        password = request.form.get("password") or ""

        limited, failures = is_rate_limited(db, identifier)
        if limited:
            log.warning(
                "Login fuer %s von %s blockiert (%s Fehlversuche)",
                identifier, client_ip(), failures,
            )
            error = (
                f"Zu viele Fehlversuche. Bitte warte {15} Minuten oder "
                "setze das Passwort zurueck."
            )
            audit(db, "login.blocked", target=identifier, actor=identifier)
        elif not identifier or not password:
            error = "Bitte Benutzername/E-Mail und Passwort eingeben."
        else:
            admin = db.execute(
                select(AdminUser).where(
                    or_(
                        func.lower(AdminUser.username) == identifier.lower(),
                        func.lower(AdminUser.email) == identifier.lower(),
                    )
                )
            ).scalars().first()
            if admin and admin.is_active and verify_password(admin.password_hash, password):
                record_login_attempt(db, identifier, True)
                admin.failed_login_count = 0
                admin.locked_until = None
                admin.last_login_at = utcnow()
                db.commit()
                init_session(admin)
                g.admin = admin
                audit(db, "login.success", target=admin.username, actor=admin.username)
                flash("Willkommen zurueck, %s." % admin.username, "success")
                return redirect(_next_target())
            record_login_attempt(db, identifier, False)
            audit(db, "login.failed", target=identifier, actor=identifier)
            left = remaining_attempts(db, identifier)
            error = "Benutzername/E-Mail oder Passwort ist falsch."
            if left <= 2:
                error += f" Noch {left} Versuch(e) uebrig."

    return render_template(
        "auth/login.html",
        error=error,
        identifier=identifier,
        app_name=settings.app_name,
        max_attempts=MAX_LOGIN_ATTEMPTS,
        session_minutes=SESSION_TIMEOUT_MINUTES,
    )


@bp.route("/logout", methods=["POST"])
@login_required
def logout():
    validate_csrf()
    db = g.db
    admin = getattr(g, "admin", None)
    audit(db, "logout", target=admin.username if admin else None,
          actor=admin.username if admin else None)
    session.clear()
    session[SESSION_KEY_WIZARD] = None
    flash("Du wurdest abgemeldet.", "info")
    return redirect(url_for("auth.login"))


@bp.route("/profil/password", methods=["GET", "POST"])
@login_required
def change_password():
    validate_csrf()
    db = g.db
    admin = g.admin
    error = ""
    if request.method == "POST":
        current = request.form.get("current_password") or ""
        new = request.form.get("new_password") or ""
        confirm = request.form.get("confirm_password") or ""
        if not verify_password(admin.password_hash, current):
            error = "Das aktuelle Passwort ist falsch."
        elif new != confirm:
            error = "Die neuen Passwoerter stimmen nicht ueberein."
        else:
            problems = password_problems(new, admin.username, admin.email)
            if problems:
                error = " ".join(problems)
            elif verify_password(admin.password_hash, new):
                error = "Das neue Passwort muss sich vom alten unterscheiden."
            else:
                admin.password_hash = hash_password(new)
                admin.failed_login_count = 0
                db.commit()
                audit(db, "password.changed", target=admin.username, actor=admin.username)
                session.clear()
                init_session(admin)
                flash("Passwort geaendert. Bitte melde dich erneut an.", "success")
                return redirect(url_for("auth.login"))

    return render_template(
        "auth/change_password.html",
        error=error,
        admin=admin,
        password_min_length=10,
    )


@bp.route("/healthz")
def healthz():
    """Liveness probe used by systemd / monitoring. No auth, no data."""
    from sqlalchemy import text

    db = g.db
    try:
        db.execute(text("SELECT 1"))
        database = "ok"
    except Exception:  # noqa: BLE001
        database = "fehler"
    from ..scheduler import get_scheduler

    scheduler = get_scheduler()
    status = 200 if database == "ok" else 503
    return {
        "status": "ok" if database == "ok" else "error",
        "database": database,
        "scheduler": bool(scheduler and scheduler.running),
        "version": current_app.config.get("APP_VERSION"),
    }, status
