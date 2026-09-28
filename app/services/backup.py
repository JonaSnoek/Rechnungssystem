"""Backups.

Two flavours:

``json``  portable, human readable, **without** any secret (the SMTP password
          and the session key are never part of the database, so they cannot
          leak). This is the recommended backup.
``sqlite`` byte copy of the SQLite database file (requires SQLite backend).

Restoring is supported for the JSON format, which is what the admin UI uses.
"""

from __future__ import annotations

import io
import json
import logging
import os
import shutil
import zipfile
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any

from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

from ..db import get_engine
from ..models import (
    AdminUser,
    AppMeta,
    BillingRun,
    Consumption,
    EmailLog,
    Invoice,
    InvoiceItem,
    Person,
    Product,
    SchedulerState,
    Setting,
    SetupState,
)
from ..settings_service import Settings

log = logging.getLogger(__name__)

BACKUP_VERSION = 1

# Secret columns that must never end up in an export.
SENSITIVE_COLUMNS = {"password_hash", "totp_secret"}

ENTITY_ORDER = [
    (Person, "persons"),
    (Product, "products"),
    (Invoice, "invoices"),
    (InvoiceItem, "invoice_items"),
    (Consumption, "consumptions"),
    (Setting, "settings"),
    (AdminUser, "admin_users"),
    (BillingRun, "billing_runs"),
    (EmailLog, "email_log"),
    (SetupState, "setup_state"),
    (AppMeta, "app_meta"),
    (SchedulerState, "scheduler_state"),
]


def _serialise(value: Any) -> Any:
    if isinstance(value, datetime):
        return {"__dt__": value.isoformat()}
    if isinstance(value, date):
        return {"__date__": value.isoformat()}
    if isinstance(value, Enum):
        return {"__enum__": value.value}
    if isinstance(value, bytes):
        return {"__bytes__": value.hex()}
    return value


def _deserialise(value: Any) -> Any:
    if isinstance(value, dict):
        if "__dt__" in value:
            return datetime.fromisoformat(value["__dt__"])
        if "__date__" in value:
            return date.fromisoformat(value["__date__"])
        if "__enum__" in value:
            return value["__enum__"]
        if "__bytes__" in value:
            return bytes.fromhex(value["__bytes__"])
    return value


def build_backup(session: Session, *, include_admin: bool = True) -> dict:
    data: dict[str, Any] = {
        "format": "paypal-payment-system-backup",
        "version": BACKUP_VERSION,
        "created_at": datetime.utcnow().isoformat(),
        "secrets_included": False,
        "note": "Passwoerter und SMTP-Zugangsdaten sind bewusst nicht enthalten.",
        "data": {},
    }
    for model, name in ENTITY_ORDER:
        if model is AdminUser and not include_admin:
            continue
        rows = []
        for obj in session.execute(select(model)).scalars().all():
            record = {}
            for column in inspect(model).columns:
                if column.name in SENSITIVE_COLUMNS:
                    record[column.name] = "<ausgelassen>"
                    continue
                record[column.name] = _serialise(getattr(obj, column.name))
            rows.append(record)
        data["data"][name] = rows
    return data


def backup_to_json(session: Session, *, include_admin: bool = True) -> str:
    return json.dumps(build_backup(session, include_admin=include_admin), indent=2, ensure_ascii=False)


def restore_from_json(session: Session, payload: str | dict, *, mode: str = "replace") -> dict:
    """Restore a JSON backup.

    ``mode='replace'`` wipes the business tables first, ``mode='merge'`` keeps
    existing rows. Admin password hashes are never restored.
    """
    data = json.loads(payload) if isinstance(payload, str) else payload
    if data.get("format") != "paypal-payment-system-backup":
        raise ValueError("Unbekanntes Backup-Format")
    if int(data.get("version", 0)) > BACKUP_VERSION:
        raise ValueError("Backup wurde mit einer neueren Version erstellt")

    counts: dict[str, int] = {}
    models = dict(ENTITY_ORDER)
    if mode == "replace":
        for model in [Consumption, InvoiceItem, Invoice, Product, Person, Setting]:
            for obj in session.execute(select(model)).scalars().all():
                session.delete(obj)
        session.flush()

    # order matters: parents before children
    for model, name in ENTITY_ORDER:
        rows = data.get("data", {}).get(name)
        if not rows:
            continue
        if model is AdminUser:
            counts[name] = 0  # password hashes are never restored
            continue
        existing = {
            getattr(obj, "id")
            for obj in session.execute(select(model)).scalars().all()
        } if mode == "merge" else set()
        for record in rows:
            values = {k: _deserialise(v) for k, v in record.items() if k not in SENSITIVE_COLUMNS}
            obj_id = values.pop("id", None)
            if mode == "merge" and obj_id in existing:
                continue
            obj = model(**values)
            if obj_id is not None:
                obj.id = obj_id
            session.merge(obj)
            counts[name] = counts.get(name, 0) + 1
    session.commit()
    return counts


def backup_sqlite_file(destination: Path) -> Path:
    engine = get_engine()
    if engine.dialect.name != "sqlite":
        raise RuntimeError(
            "Rohes Datei-Backup wird nur bei SQLite unterstuetzt. "
            "Nutze bei PostgreSQL 'pg_dump' oder das JSON-Backup."
        )
    raw = engine.url.database
    if not raw:
        raise RuntimeError("SQLite-Datenbankdatei nicht ermittelbar")
    source = Path(raw)
    if not source.exists():
        raise RuntimeError(f"SQLite-Datei nicht gefunden: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    # consistent copy even while the app is running
    with open(source, "rb") as src, open(destination, "wb") as dst:
        src_data = src.read()
        if source.with_name(source.name + "-wal").exists():
            wal = source.with_name(source.name + "-wal")
            dst_data = _merge_wal(source, wal, src_data)
        else:
            dst_data = src_data
        dst.write(dst_data)
    return destination


def _merge_wal(source: Path, wal: Path, main_bytes: bytes) -> bytes:
    """Best-effort WAL merge using sqlite's own backup API."""
    import sqlite3
    import tempfile

    tmpdir = Path(tempfile.mkdtemp(prefix="pps-backup-"))
    try:
        copy_main = tmpdir / source.name
        copy_main.write_bytes(main_bytes)
        copy_wal = tmpdir / (source.name + "-wal")
        copy_wal.write_bytes(wal.read_bytes())
        conn = sqlite3.connect(str(copy_main))
        try:
            merged = io.BytesIO()
            dest = sqlite3.connect(str(tmpdir / "out.db"))
            try:
                conn.backup(dest)
                dest.close()
                return (tmpdir / "out.db").read_bytes()
            finally:
                dest.close()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        log.warning("WAL-Merge fehlgeschlagen, es wird die Hauptdatei kopiert")
        return main_bytes
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def backup_zip(session: Session, settings: Settings, *, include_admin: bool = True) -> bytes:
    """A zip containing the JSON backup plus human readable CSV reports."""
    from .export import consumptions_csv, invoices_csv

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "backup.json",
            backup_to_json(session, include_admin=include_admin),
        )
        archive.writestr("verzehr.csv", consumptions_csv(session, settings))
        archive.writestr("abrechnungen.csv", invoices_csv(session, settings))
        archive.writestr(
            "INFO.txt",
            "PayPal Verzehrabrechnungssystem - Backup\n"
            f"Erstellt: {datetime.now().isoformat(timespec='seconds')}\n"
            f"Version: {BACKUP_VERSION}\n\n"
            "Enthalten: Personen, Produkte, Verzehrbuchungen, Abrechnungen, "
            "Einstellungen, Betriebsdaten.\n"
            "NICHT enthalten: SMTP-Passwort, Session-Schluessel, "
            "Admin-Passwort-Hashes.\n\n"
            "Wiederherstellung: Administration -> System -> Backup -> "
            "Aus JSON-Backup wiederherstellen\n",
        )
    return buffer.getvalue()


def write_backup_to_disk(
    session: Session, settings: Settings, directory: Path, prefix: str = "backup"
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = directory / f"{prefix}-{stamp}.json"
    target.write_text(backup_to_json(session), encoding="utf-8")
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass
    return target
