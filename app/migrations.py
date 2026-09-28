"""Minimal, dependency-free SQL migration runner.

Migrations are plain ``.sql`` files in ``migrations/`` named
``NNN_description.sql``. They are applied in numeric order and recorded in the
``schema_migrations`` table, so re-running is a no-op.

Only forward-only migrations are supported, which is exactly what this project
needs and keeps the deployment story simple.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import text

from .db import get_engine

log = logging.getLogger(__name__)

_FILENAME_RE = re.compile(r"^(\d{3,})_([A-Za-z0-9_\-]+)\.sql$")
VERSION_TABLE = "schema_migrations"


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    path: Path

    @property
    def label(self) -> str:
        return f"{self.version:03d}_{self.name}"


def _split_statements(script: str) -> list[str]:
    """Split a SQL script on semicolons, respecting strings and comments."""
    statements: list[str] = []
    buffer: list[str] = []
    in_single = False
    in_double = False
    i = 0
    length = len(script)
    while i < length:
        char = script[i]
        nxt = script[i + 1] if i + 1 < length else ""
        if in_single:
            buffer.append(char)
            if char == "'":
                if nxt == "'":
                    buffer.append(nxt)
                    i += 2
                    continue
                in_single = False
            i += 1
            continue
        if in_double:
            buffer.append(char)
            if char == '"':
                in_double = False
            i += 1
            continue
        if char == "-" and nxt == "-":
            while i < length and script[i] != "\n":
                i += 1
            continue
        if char == "/" and nxt == "*":
            i += 2
            while i + 1 < length and not (script[i] == "*" and script[i + 1] == "/"):
                i += 1
            i += 2
            continue
        if char == "'":
            in_single = True
            buffer.append(char)
            i += 1
            continue
        if char == '"':
            in_double = True
            buffer.append(char)
            i += 1
            continue
        if char == ";":
            statements.append("".join(buffer).strip())
            buffer = []
            i += 1
            continue
        buffer.append(char)
        i += 1
    tail = "".join(buffer).strip()
    if tail:
        statements.append(tail)
    return [s for s in statements if s]


def discover(directory: Path) -> list[Migration]:
    migrations: list[Migration] = []
    if not directory.exists():
        return migrations
    for path in sorted(directory.glob("*.sql")):
        match = _FILENAME_RE.match(path.name)
        if not match:
            log.warning("Ueberspringe Migrationsdatei mit ungueltigem Namen: %s", path.name)
            continue
        migrations.append(
            Migration(version=int(match.group(1)), name=match.group(2), path=path)
        )
    migrations.sort(key=lambda m: (m.version, m.name))
    return migrations


def _ensure_version_table() -> None:
    engine = get_engine()
    dialect = engine.dialect.name
    with engine.begin() as conn:
        if dialect == "sqlite":
            conn.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS schema_migrations ("
                    "version INTEGER PRIMARY KEY, name VARCHAR(255) NOT NULL, "
                    "applied_at TIMESTAMP NOT NULL)"
                )
            )
        else:
            conn.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS schema_migrations ("
                    "version INTEGER PRIMARY KEY, name VARCHAR(255) NOT NULL, "
                    "applied_at TIMESTAMP NOT NULL)"
                )
            )


def applied_versions() -> set[int]:
    _ensure_version_table()
    with get_engine().connect() as conn:
        rows = conn.execute(text(f"SELECT version FROM {VERSION_TABLE}")).fetchall()
    return {int(row[0]) for row in rows}


def current_version() -> int:
    versions = applied_versions()
    return max(versions) if versions else 0


def pending(directory: Path) -> list[Migration]:
    done = applied_versions()
    return [m for m in discover(directory) if m.version not in done]


def _record(conn, migration: Migration) -> None:
    from datetime import datetime

    conn.execute(
        text(
            f"INSERT INTO {VERSION_TABLE} (version, name, applied_at) "
            "VALUES (:version, :name, :applied_at)"
        ),
        {
            "version": migration.version,
            "name": migration.name,
            "applied_at": datetime.utcnow(),
        },
    )


def apply_migration(conn, migration: Migration) -> None:
    script = migration.path.read_text(encoding="utf-8")
    statements = _split_statements(script)
    if not statements:
        _record(conn, migration)
        return
    for statement in statements:
        conn.execute(text(statement))
    _record(conn, migration)


def run_migrations(directory: Path, *, target: int | None = None) -> list[str]:
    """Apply all pending migrations. Returns the labels that were applied."""
    _ensure_version_table()
    engine = get_engine()
    todo = [m for m in pending(directory) if target is None or m.version <= target]
    applied: list[str] = []
    for migration in todo:
        log.info("Migration wird ausgefuehrt: %s", migration.label)
        with engine.begin() as conn:
            apply_migration(conn, migration)
        applied.append(migration.label)
    if not applied:
        log.info("Keine offenen Migrationen")
    return applied


def status(directory: Path) -> dict[str, list[dict]]:
    done = applied_versions()
    result: dict[str, list[dict]] = {"applied": [], "pending": []}
    for migration in discover(directory):
        entry = {
            "version": migration.version,
            "name": migration.name,
            "label": migration.label,
        }
        if migration.version in done:
            result["applied"].append(entry)
        else:
            result["pending"].append(entry)
    return result
