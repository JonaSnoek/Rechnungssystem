"""Management CLI: ``python manage.py <command>``."""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

from app import create_app
from app.config import INSTANCE_DIR, MIGRATIONS_DIR
from app import migrations
from app.db import session_scope
from app.models import AdminUser
from app.security import hash_password, password_problems
from app.settings_service import Settings
from app.secrets_store import get_store


def _app(start_scheduler: bool = False):
    return create_app(start_scheduler=start_scheduler)


def cmd_migrate(args) -> int:
    app = _app()
    with app.app_context():
        applied = migrations.run_migrations(MIGRATIONS_DIR)
    if applied:
        for label in applied:
            print(f"  ausgefuehrt: {label}")
    else:
        print("Keine offenen Migrationen.")
    return 0


def cmd_initdb(args) -> int:
    app = _app()
    with app.app_context():
        applied = migrations.run_migrations(MIGRATIONS_DIR)
        with session_scope() as session:
            count = session.query(AdminUser).count()
        print(f"Datenbank bereit ({len(applied)} Migration(en) ausgefuehrt).")
        print(f"Administratoren: {count}")
        if count == 0:
            print("Noch kein Administrator angelegt -> bitte 'setup' im Web aufrufen")
    return 0


def cmd_status(args) -> int:
    app = _app()
    with app.app_context():
        from app.db import get_engine
        from app.scheduler import scheduler_snapshot
        from app.services.stats import dashboard_stats

        db = session_scope_gen()
        settings = Settings(db)
        stats = dashboard_stats(db, settings)
        snap = scheduler_snapshot(db)
        print("Status")
        print("-" * 40)
        print(f"Version            : {app.config.get('APP_VERSION')}")
        print(f"Datenbank          : {get_engine().dialect.name}")
        print(f"Schema             : {migrations.current_version()}")
        print(f"Einrichtung        : {'abgeschlossen' if settings.setup_completed else 'offen'}")
        print(f"Offene Personen    : {stats.open_persons}")
        print(f"Offener Betrag     : {stats.open_total_cents / 100:.2f} {settings.currency}")
        print(f"Offene Rechnungen  : {stats.open_invoices}")
        print(f"Fehlgeschlagen     : {stats.failed_invoices}")
        print(f"Scheduler laeuft   : {'ja' if snap['running'] else 'nein'}")
        print(f"Abrechnung         : {'aktiv' if snap['enabled'] else 'aus'} um "
              f"{snap['billing_time']} ({snap['timezone']})")
        print(f"Naechste Abrechnung: {snap['next_run_at']}")
        print(f"Letzter Lauf       : {snap['last_billed_period']}")
        if snap["last_error"]:
            print(f"Letzter Fehler     : {snap['last_error']}")
        remove()
    return 0


def session_scope_gen():
    from app.db import get_session

    return get_session()


def remove():
    from app.db import remove_session

    remove_session()


def cmd_create_admin(args) -> int:
    app = _app()
    with app.app_context():
        username = args.username
        email = args.email
        password = args.password or getpass.getpass("Passwort: ")
        problems = password_problems(password, username, email)
        if problems:
            for problem in problems:
                print(f"FEHLER: {problem}", file=sys.stderr)
            return 1
        with session_scope() as session:
            existing = session.query(AdminUser).filter(AdminUser.username == username).first()
            if existing:
                existing.password_hash = hash_password(password)
                existing.email = email
                existing.is_active = True
                print(f"Administrator {username} aktualisiert.")
            else:
                session.add(
                    AdminUser(
                        username=username,
                        email=email,
                        password_hash=hash_password(password),
                        is_active=True,
                    )
                )
                print(f"Administrator {username} angelegt.")
    return 0


def cmd_change_password(args) -> int:
    app = _app()
    with app.app_context():
        password = args.password or getpass.getpass("Neues Passwort: ")
        with session_scope() as session:
            admin = session.query(AdminUser).filter(AdminUser.username == args.username).first()
            if admin is None:
                print(f"FEHLER: Benutzer {args.username} nicht gefunden", file=sys.stderr)
                return 1
            problems = password_problems(password, admin.username, admin.email)
            if problems:
                for problem in problems:
                    print(f"FEHLER: {problem}", file=sys.stderr)
                return 1
            admin.password_hash = hash_password(password)
            admin.failed_login_count = 0
            print(f"Passwort fuer {args.username} geaendert.")
    return 0


def cmd_backup(args) -> int:
    from app.services import backup as backup_service

    app = _app()
    with app.app_context():
        with session_scope() as session:
            settings = Settings(session)
            target = backup_service.write_backup_to_disk(
                session, settings, Path(args.directory or (INSTANCE_DIR / "backups"))
            )
            print(f"Backup geschrieben: {target}")
    return 0


def cmd_check(args) -> int:
    """Pre-flight checks, useful before/after an update."""
    ok = True
    app = _app()
    with app.app_context():
        from app.db import get_engine
        from app.services.mail_factory import is_email_configured

        with session_scope() as session:
            settings = Settings(session)
            print("Konfiguration")
            print("-" * 40)
            print(f"APP_ENV            : {os.environ.get('APP_ENV', 'production')}")
            print(f"Secret Key gesetzt : {'ja' if get_store().has('SECRET_KEY') or app.config.get('SECRET_KEY') else 'NEIN'}")
            print(f"PayPal-Benutzer    : {settings.paypal_username or 'FEHLT'}")
            print(f"SMTP konfiguriert  : {'ja' if is_email_configured(settings) else 'NEIN'}")
            print(f"SMTP-Passwort      : {'ja' if get_store().has('SMTP_PASSWORD') else 'NEIN'}")
            print(f"Abrechnung         : {'aktiv' if settings.auto_billing_enabled else 'inaktiv'} um {settings.auto_billing_time} ({settings.timezone})")
            try:
                with get_engine().connect() as conn:
                    conn.exec_driver_sql("SELECT 1")
                print(f"Datenbank          : erreichbar")
            except Exception as exc:  # noqa: BLE001
                ok = False
                print(f"Datenbank          : FEHLER {exc}")
            if not settings.paypal_username:
                ok = False
                print("WARNUNG: Kein PayPal.Me-Benutzername konfiguriert.")
            if not is_email_configured(settings):
                ok = False
                print("WARNUNG: SMTP nicht vollstaendig konfiguriert.")
    return 0 if ok else 1


def cmd_secret(args) -> int:
    store = get_store()
    if args.action == "show":
        for key in ("SECRET_KEY", "SMTP_PASSWORD"):
            value = store.get(key)
            print(f"{key}: {(value[:6] + '...' + value[-4:]) if value and len(value) > 12 else ('gesetzt' if value else 'NICHT GESETZT')}")
    elif args.action == "set":
        if not args.value:
            args.value = getpass.getpass("Wert: ")
        store.set(args.key.upper().replace("SMTP_PASSWORD", "SMTP_PASSWORD"), args.value)
        print(f"{args.key} gespeichert in {store.path}")
    elif args.action == "rotate-key":
        store.set("SECRET_KEY", store.generate(48))
        print("Neuer SECRET_KEY erzeugt. Alle Sitzungen werden abgemeldet.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="manage.py", description="Verwaltung fuer das Verzehrabrechnungssystem"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("initdb", help="Datenbank initialisieren / Migrationen ausfuehren")
    sub.add_parser("migrate", help="Offene Migrationen ausfuehren")
    sub.add_parser("status", help="Systemstatus anzeigen")
    sub.add_parser("check", help="Konfiguration pruefen")

    p = sub.add_parser("create-admin", help="Administrator anlegen oder Passwort zuruecksetzen")
    p.add_argument("username")
    p.add_argument("email")
    p.add_argument("--password", help="sonst interaktiv abfragen")

    p = sub.add_parser("change-password", help="Passwort aendern")
    p.add_argument("username")
    p.add_argument("--password")

    p = sub.add_parser("backup", help="JSON-Backup erstellen")
    p.add_argument("--directory")

    p = sub.add_parser("secret", help="Secrets verwalten")
    p.add_argument("action", choices=["show", "set", "rotate-key"])
    p.add_argument("key", nargs="?")
    p.add_argument("value", nargs="?")

    args = parser.parse_args(argv)
    handlers = {
        "initdb": cmd_initdb,
        "migrate": cmd_migrate,
        "status": cmd_status,
        "create-admin": cmd_create_admin,
        "change-password": cmd_change_password,
        "backup": cmd_backup,
        "check": cmd_check,
        "secret": cmd_secret,
    }
    try:
        return handlers[args.command](args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
