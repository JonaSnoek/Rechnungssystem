"""Security helpers: password hashing, CSRF tokens, login rate limiting, audit log."""

from __future__ import annotations

import hmac
import logging
import secrets
from datetime import datetime, timedelta

from flask import abort, g, redirect, request, session, url_for
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session
from werkzeug.security import check_password_hash, generate_password_hash

from .models import AuditLog, AdminUser, LoginAttempt, utcnow

log = logging.getLogger(__name__)

SESSION_KEY_ADMIN_ID = "admin_id"
SESSION_KEY_CSRF = "csrf_token"
SESSION_KEY_LOGIN_AT = "login_at"
SESSION_KEY_FINGERPRINT = "fingerprint"
SESSION_KEY_WIZARD = "wizard"

SESSION_TIMEOUT_MINUTES = 120
MAX_LOGIN_ATTEMPTS = 5
LOCKOUT_MINUTES = 15
PASSWORD_MIN_LENGTH = 10


# ---------------------------------------------------------------------------
# passwords
# ---------------------------------------------------------------------------
def hash_password(password: str) -> str:
    return generate_password_hash(password, method="pbkdf2:sha256:600000")


def verify_password(password_hash: str, password: str) -> bool:
    if not password_hash or not password:
        return False
    try:
        return check_password_hash(password_hash, password)
    except (ValueError, TypeError):
        return False


def password_problems(password: str, username: str = "", email: str = "") -> list[str]:
    problems: list[str] = []
    if not password:
        return ["Passwort fehlt"]
    if len(password) < PASSWORD_MIN_LENGTH:
        problems.append(
            f"Passwort muss mindestens {PASSWORD_MIN_LENGTH} Zeichen lang sein"
        )
    if len(password) > 200:
        problems.append("Passwort ist zu lang (max. 200 Zeichen)")
    classes = 0
    classes += any(c.islower() for c in password)
    classes += any(c.isupper() for c in password)
    classes += any(c.isdigit() for c in password)
    classes += any(not c.isalnum() for c in password)
    if classes < 3:
        problems.append(
            "Passwort braucht mindestens drei von: Kleinbuchstaben, Grossbuchstaben, "
            "Ziffern, Sonderzeichen"
        )
    lowered = password.lower()
    if username and username.lower() in lowered:
        problems.append("Passwort darf den Benutzernamen nicht enthalten")
    local_part = email.split("@")[0].lower()
    if local_part and len(local_part) > 2 and local_part in lowered:
        problems.append("Passwort darf keinen Teil der E-Mail-Adresse enthalten")
    common = {"passwort1234", "1234567890", "qwertyuiop", "adminadmin", "letmein123"}
    if lowered in common:
        problems.append("Dieses Passwort ist zu leicht erratbar")
    return problems


# ---------------------------------------------------------------------------
# session / CSRF
# ---------------------------------------------------------------------------
def client_fingerprint() -> str:
    agent = request.headers.get("User-Agent", "")[:200]
    return f"{agent}|{request.headers.get('Accept-Language', '')[:40]}"


def init_session(admin: AdminUser) -> None:
    session.clear()
    session.permanent = True
    session[SESSION_KEY_ADMIN_ID] = admin.id
    session[SESSION_KEY_CSRF] = secrets.token_urlsafe(32)
    session[SESSION_KEY_LOGIN_AT] = utcnow().isoformat()
    session[SESSION_KEY_FINGERPRINT] = hmac.new(
        _fingerprint_key(), agent_or_empty(), "sha256"
    ).hexdigest()


def agent_or_empty() -> bytes:
    return request.headers.get("User-Agent", "").encode("utf-8", "replace")


def _fingerprint_key() -> bytes:
    from .secrets_store import get_store

    return (get_store().get("SECRET_KEY") or "dev").encode("utf-8")


def current_admin(db: Session) -> AdminUser | None:
    admin_id = session.get(SESSION_KEY_ADMIN_ID)
    if not admin_id:
        return None
    admin = db.get(AdminUser, int(admin_id))
    if admin is None or not admin.is_active:
        return None
    return admin


def session_valid(db: Session) -> bool:
    """Validate session age and user agent binding."""
    logged_in_at = session.get(SESSION_KEY_LOGIN_AT)
    if not logged_in_at:
        return False
    try:
        age = utcnow() - datetime.fromisoformat(logged_in_at)
    except ValueError:
        return False
    if age > timedelta(minutes=SESSION_TIMEOUT_MINUTES):
        return False
    stored = session.get(SESSION_KEY_FINGERPRINT)
    expected = hmac.new(_fingerprint_key(), agent_or_empty(), "sha256").hexdigest()
    if stored and not hmac.compare_digest(str(stored), expected):
        return False
    return current_admin(db) is not None


def csrf_token() -> str:
    token = session.get(SESSION_KEY_CSRF)
    if not token:
        token = secrets.token_urlsafe(32)
        session[SESSION_KEY_CSRF] = token
    return token


def validate_csrf() -> None:
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return
    sent = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token", "")
    expected = session.get(SESSION_KEY_CSRF, "")
    if not sent or not expected or not hmac.compare_digest(str(sent), str(expected)):
        log.warning("CSRF-Pruefung fehlgeschlagen fuer %s", request.path)
        abort(400, description="Ungueltiges CSRF-Token. Bitte Seite neu laden.")


# ---------------------------------------------------------------------------
# rate limiting
# ---------------------------------------------------------------------------
def record_login_attempt(db: Session, identifier: str, success: bool) -> None:
    db.add(
        LoginAttempt(
            identifier=(identifier or "")[:160].lower(),
            ip_address=client_ip()[:64],
            success=success,
            attempted_at=utcnow(),
        )
    )
    purge_old_attempts(db)
    db.commit()


def purge_old_attempts(db: Session, hours: int = 48) -> None:
    cutoff = utcnow() - timedelta(hours=hours)
    db.execute(delete(LoginAttempt).where(LoginAttempt.attempted_at < cutoff))


def recent_failures(db: Session, identifier: str) -> int:
    cutoff = utcnow() - timedelta(minutes=LOCKOUT_MINUTES)
    return int(
        db.execute(
            select(func.count(LoginAttempt.id)).where(
                LoginAttempt.identifier == (identifier or "").lower()[:160],
                LoginAttempt.success.is_(False),
                LoginAttempt.attempted_at >= cutoff,
            )
        ).scalar_one()
        or 0
    )


def is_rate_limited(db: Session, identifier: str) -> tuple[bool, int]:
    failures = recent_failures(db, identifier)
    return failures >= MAX_LOGIN_ATTEMPTS, failures


def remaining_attempts(db: Session, identifier: str) -> int:
    return max(0, MAX_LOGIN_ATTEMPTS - recent_failures(db, identifier))


def client_ip() -> str:
    # Only trust X-Forwarded-For when explicitly enabled by the operator.
    from flask import current_app

    if current_app.config.get("TRUST_PROXY", False):
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.remote_addr or "0.0.0.0"


# ---------------------------------------------------------------------------
# audit
# ---------------------------------------------------------------------------
def audit(db: Session, action: str, target: str | None = None, detail: str | None = None, actor: str | None = None) -> None:
    if actor is None:
        admin = getattr(g, "admin", None) if hasattr(g, "admin") else None
        actor = admin.username if admin else "system"
    try:
        db.add(
            AuditLog(
                actor=actor,
                action=action[:80],
                target=(target or "")[:160] or None,
                detail=(detail or "")[:2000] or None,
                ip_address=client_ip()[:64],
                created_at=utcnow(),
            )
        )
        db.commit()
    except Exception:  # noqa: BLE001 - auditing must never break a request
        log.warning("Audit-Log-Eintrag fehlgeschlagen: %s", action)
        db.rollback()


def login_required(view):
    from functools import wraps

    @wraps(view)
    def wrapper(*args, **kwargs):
        from flask import current_app, flash

        db = current_app.extensions["db_session"]
        if not session_valid(db):
            if session.get(SESSION_KEY_ADMIN_ID):
                session.clear()
                flash("Sitzung abgelaufen. Bitte erneut anmelden.", "warning")
            return redirect(url_for("auth.login", next=request.full_path))
        return view(*args, **kwargs)

    return wrapper


def setup_required(view):
    """Redirect to the setup wizard while it has not been completed."""
    from functools import wraps

    @wraps(view)
    def wrapper(*args, **kwargs):
        from flask import current_app

        db = current_app.extensions["db_session"]
        from .settings_service import SETUP_COMPLETED, Settings

        if not Settings(db).get_bool(SETUP_COMPLETED, False):
            return redirect(url_for("setup.index"))
        return view(*args, **kwargs)

    return wrapper
