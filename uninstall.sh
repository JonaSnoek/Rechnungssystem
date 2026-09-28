#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Verzehrabrechnung - Deinstallation
#
#   sudo ./uninstall.sh            # Dienste und Benutzer entfernen, Daten behalten
#   sudo ./uninstall.sh --purge    # zusätzlich instance/ und var/ löschen
#
# --purge ist endgültig: es werden Datenbank und Backups gelöscht.
# Deshalb wird vorher ausdrücklich nachgefragt.
# ---------------------------------------------------------------------------
set -Eeuo pipefail

APP_DIR="${APP_DIR:-/opt/verzehr}"
APP_USER="${APP_USER:-verzehr}"
SERVICE_WEB="verzehr-web.service"
SERVICE_SCHED="verzehr-scheduler.service"
PURGE=0
CONFIRM="${2:-}"
if [ "${1:-}" = "--purge" ]; then
  PURGE=1
fi

say()  { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m  %s\n' "$*" >&2; }
die()  { printf '\033[1;31mxx\033[0m  %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Bitte mit sudo ausfuehren."

# --- 1. Sicherheitsabfrage vor dem Loeschen --------------------------------
if [ "$PURGE" -eq 1 ]; then
  warn "PURGE: Datenbank, Secrets und Backups werden UNWIDERRUFLICH geloescht."
  if [ -z "$CONFIRM" ] && [ -t 0 ]; then
    printf 'Zum Bestaetigen exakt "LOESCHEN" eingeben: '
    read -r CONFIRM
  fi
  [ "$CONFIRM" = "LOESCHEN" ] \
    || die "Abgebrochen. Zum Fortfahren nicht-interaktiv: sudo ./uninstall.sh --purge LOESCHEN"
fi

# --- 2. Dienste stoppen und entfernen --------------------------------------
for svc in "$SERVICE_SCHED" "$SERVICE_WEB"; do
  if systemctl list-unit-files | grep -q "^${svc}"; then
    say "Stoppe und deaktiviere ${svc}"
    systemctl stop  "$svc" || true
    systemctl disable "$svc" || true
    rm -f "/etc/systemd/system/${svc}"
  fi
done
systemctl daemon-reload
systemctl reset-failed 2>/dev/null || true

# --- 3. nginx --------------------------------------------------------------
for f in /etc/nginx/sites-enabled/verzehr.conf \
         /etc/nginx/sites-available/verzehr.conf; do
  [ -e "$f" ] || continue
  say "Entferne nginx-Konfiguration $f"
  rm -f "$f"
done
if command -v nginx >/dev/null 2>&1; then
  nginx -t >/dev/null 2>&1 && systemctl reload nginx || true
fi

# --- 4. Dateien ------------------------------------------------------------
if [ "$PURGE" -eq 1 ]; then
  say "Loesche $APP_DIR"
  rm -rf "$APP_DIR"
else
  say "Entferne Programmcode, behalte instance/ und var/"
  cd "$APP_DIR" 2>/dev/null || true
  find . -maxdepth 1 -mindepth 1 \
    ! -name instance ! -name var ! -name '.env' -exec rm -rf {} +
  say "Daten bleiben erhalten unter: $APP_DIR/instance und $APP_DIR/var"
  say "Komplett entfernen mit: sudo ./uninstall.sh --purge LOESCHEN"
fi

# --- 5. Benutzer -----------------------------------------------------------
if id -u "$APP_USER" >/dev/null 2>&1; then
  say "Entferne System-Benutzer $APP_USER"
  userdel "$APP_USER" 2>/dev/null || warn "Benutzer konnte nicht entfernt werden."
fi

say "Fertig."
