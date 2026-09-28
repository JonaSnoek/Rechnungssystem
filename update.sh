#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Verzehrabrechnung - Update
#
#   sudo ./update.sh
#
# Legt vor dem Update automatisch ein Datenbank-Backup an, installiert die
# Abhaengigkeiten neu, fuehrt offene Migrationen aus und startet beide Dienste
# neu. Laeuft die Anwendung bereits, wird sie ersetzt; der Scheduler wartet,
# bis der Web-Dienst wieder bereit ist.
# ---------------------------------------------------------------------------
set -Eeuo pipefail

APP_DIR="${APP_DIR:-/opt/verzehr}"
APP_USER="${APP_USER:-verzehr}"
SERVICE_WEB="verzehr-web.service"
SERVICE_SCHED="verzehr-scheduler.service"

say()  { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m  %s\n' "$*" >&2; }
die()  { printf '\033[1;31mxx\033[0m  %s\n' "$*" >&2; exit 1; }
trap 'die "Abbruch bei Zeile $LINENO"' ERR

# Befehl als System-Benutzer ausfuehren (siehe install.sh)
as_app_user() {
  if command -v runuser >/dev/null 2>&1; then
    runuser -u "$APP_USER" -- "$@"
  else
    local quoted
    quoted=$(printf '%q ' "$@")
    su -s /bin/sh -c "$quoted" "$APP_USER"
  fi
}

[ "$(id -u)" -eq 0 ] || die "Bitte mit sudo ausfuehren."
cd "$APP_DIR" || die "$APP_DIR nicht gefunden."

# --- 1. Backup vor dem Update ---------------------------------------------
BACKUP=""
if systemctl is-active --quiet "$SERVICE_WEB"; then
  say "Erzeuge Backup"
  BACKUP="$(as_app_user "$APP_DIR/.venv/bin/python" manage.py backup 2>/dev/null \
            | sed -n 's/^Backup geschrieben: //p' || true)"
  if [ -n "$BACKUP" ] && [ -f "$BACKUP" ]; then
    say "Backup erstellt: $BACKUP"
  else
    die "Backup fehlgeschlagen - Update abgebrochen. Bitte Datenbank von Hand sichern."
  fi
else
  warn "Web-Dienst laeuft nicht - es wird kein Backup erstellt."
fi

# --- 2. Anwendung stoppen --------------------------------------------------
say "Stoppe Scheduler"
systemctl stop "$SERVICE_SCHED"
say "Stoppe Web-Dienst"
systemctl stop "$SERVICE_WEB"

# --- 3. Neuer Quellcode ----------------------------------------------------
if [ -d .git ] && command -v git >/dev/null 2>&1; then
  say "Hole Updates aus Git"
  git fetch --quiet --all
  git pull --ff-only
else
  warn "Kein Git-Repository - bitte den Quellcode manuell aktualisieren."
fi

# --- 4. Abhaengigkeiten und Schema ----------------------------------------
say "Aktualisiere Python-Abhaengigkeiten"
"$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip wheel
"$APP_DIR/.venv/bin/pip" install --quiet -r requirements.txt

say "Fuehre offene Migrationen aus"
as_app_user "$APP_DIR/.venv/bin/python" manage.py migrate

# --- 5. Neustart -----------------------------------------------------------
say "Starte Web-Dienst"
systemctl start "$SERVICE_WEB"
for _ in $(seq 1 30); do
  if systemctl is-active --quiet "$SERVICE_WEB"; then break; fi
  sleep 1
done
systemctl is-active --quiet "$SERVICE_WEB" || die "Web-Dienst kam nicht hoch."

say "Starte Scheduler"
systemctl start "$SERVICE_SCHED"

systemctl --no-pager --lines=0 status "$SERVICE_WEB" "$SERVICE_SCHED" || true
say "Update abgeschlossen.${BACKUP:+  Backup: $BACKUP}"
