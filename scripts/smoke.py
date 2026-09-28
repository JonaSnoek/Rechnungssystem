#!/usr/bin/env python3
"""Rauchtest nach der Installation.

Startet die Anwendung im Speicher, ruft jede GET-Route auf und meldet
Fehlerseiten. Es wird keine E-Mail versendet und nichts an der Datenbank
geaendert - nur gelesen.

    python scripts/smoke.py

Exitcode 0 = alles in Ordnung, 1 = mindestens eine Fehlerseite.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Eigene Datenbank, damit der Rauchtest die echten Daten nicht anfasst.
if not os.environ.get("SMOKE_USE_REAL_DB"):
    tmp = tempfile.mkdtemp(prefix="verzehr-smoke-")
    os.environ["DATABASE_URL"] = f"sqlite:///{Path(tmp, 'smoke.db').as_posix()}"
    os.environ["SECRETS_FILE"] = str(Path(tmp, "secrets.env"))
    os.environ.setdefault("SECRET_KEY", "smoke-test-key-not-for-production")
    os.environ.setdefault("SCHEDULER_ENABLED", "0")
    os.environ.setdefault("APP_ENV", "testing")
    os.environ.setdefault("LOG_LEVEL", "ERROR")


def main() -> int:
    from app import create_app
    from app.config import Config

    app = create_app(Config(), start_scheduler=False)
    client = app.test_client()

    rules = sorted(app.url_map.iter_rules(), key=lambda r: r.rule)
    failures: list[tuple[str, int, str]] = []
    checked = 0

    print(f"{len(rules)} Routen gefunden\n")
    for rule in rules:
        if "GET" not in (rule.methods or set()):
            continue
        if rule.rule.startswith("/static"):
            continue
        if "<" in rule.rule:            # Platzhalterrouten ueberspringen
            continue
        try:
            response = client.get(rule.rule)
        except Exception as exc:          # noqa: BLE001
            failures.append((rule.rule, 0, f"{type(exc).__name__}: {exc}"))
            continue
        checked += 1
        if response.status_code >= 500:
            failures.append((rule.rule, response.status_code,
                             response.get_data(as_text=True)[:200]))
        else:
            print(f"  {response.status_code}  {rule.rule}")

    print(f"\n{checked} Routen geprueft")
    if failures:
        print(f"\n{len(failures)} Fehler:")
        for path, code, detail in failures:
            print(f"  {path} -> {code}\n    {detail}")
        return 1
    print("keine Fehler")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
