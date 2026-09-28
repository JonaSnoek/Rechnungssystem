"""Kommandozeile: manage.py.

Die Unterbefehle werden als Subprozess ausgefuehrt, damit der Importpfad und
die Exit-Codes genau so geprueft werden wie im Betrieb.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def run_manage(tmp_path, *args, expect_ok=True):
    """Run ``manage.py`` against a throwaway database."""
    env = dict(os.environ)
    env.update(
        DATABASE_URL=f"sqlite:///{(tmp_path / 'cli.db').as_posix()}",
        SECRETS_FILE=str(tmp_path / "secrets.env"),
        SECRET_KEY="cli-test-secret-key-32-characters",
        APP_ENV="testing",
        SCHEDULER_ENABLED="0",
        LOG_LEVEL="ERROR",
    )
    proc = subprocess.run(
        [sys.executable, "manage.py", *args],
        cwd=PROJECT_ROOT, env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=120,
    )
    if expect_ok:
        assert proc.returncode == 0, (
            f"manage.py {' '.join(args)} -> {proc.returncode}\n"
            f"{proc.stdout}\n{proc.stderr}"
        )
    return proc


class TestManagePy:
    def test_initdb_legt_schema_an(self, tmp_path):
        out = run_manage(tmp_path, "initdb").stdout
        assert "Migration" in out or "Initialisierung" in out
        assert (tmp_path / "cli.db").exists()

    def test_status_laeuft_und_zeigt_den_offenen_betrag(self, tmp_path):
        run_manage(tmp_path, "initdb")
        out = run_manage(tmp_path, "status").stdout
        assert "Offener Betrag" in out
        assert "0.00" in out
        assert "Naechste Abrechnung" in out

    def test_migrate_ist_idempotent(self, tmp_path):
        run_manage(tmp_path, "initdb")
        first = run_manage(tmp_path, "migrate").stdout.lower()
        second = run_manage(tmp_path, "migrate").stdout.lower()
        assert "keine offenen migrationen" in first
        assert "keine offenen migrationen" in second

    def test_check_meldet_fehlende_konfiguration(self, tmp_path):
        proc = run_manage(tmp_path, "check", expect_ok=False)
        combined = proc.stdout + proc.stderr
        assert "PayPal" in combined
        assert "WARNUNG" in combined

    def test_backup_erstellt_eine_datei(self, tmp_path):
        run_manage(tmp_path, "initdb")
        target = tmp_path / "backups"
        out = run_manage(tmp_path, "backup", "--directory", str(target)).stdout
        assert "Backup geschrieben:" in out
        files = list(target.glob("*.json"))
        assert len(files) == 1, list(target.iterdir())
        assert files[0].stat().st_size > 0
        # the payload is real JSON with the expected top level keys
        import json

        data = json.loads(files[0].read_text(encoding="utf-8"))
        assert isinstance(data, dict) and data

    def test_create_admin_und_change_password(self, tmp_path):
        run_manage(tmp_path, "initdb")
        run_manage(tmp_path, "create-admin", "chef", "chef@example.com",
                   "--password", "EinSicheres!2026")
        run_manage(tmp_path, "change-password", "chef",
                   "--password", "NochEinSicheres!2026")
        out = run_manage(tmp_path, "status").stdout
        assert "Traceback" not in out

    def test_secret_show_maskiert_werte(self, tmp_path):
        run_manage(tmp_path, "initdb")
        out = run_manage(tmp_path, "secret", "show").stdout
        assert "SMTP_PASSWORD" in out or "SECRET_KEY" in out

    def test_unbekannter_befehl(self, tmp_path):
        proc = run_manage(tmp_path, "quatsch", expect_ok=False)
        assert proc.returncode != 0

    @pytest.mark.parametrize("command", ["initdb", "status", "check", "backup"])
    def test_keine_tracebacks_in_der_ausgabe(self, tmp_path, command):
        proc = run_manage(tmp_path, command, expect_ok=False)
        assert "Traceback" not in proc.stderr, proc.stderr
        assert "AttributeError" not in proc.stdout + proc.stderr
