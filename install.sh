#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Verzehrabrechnung - Installation
#
#   sudo ./install.sh
#
# Legt an: System-Benutzer, virtuelles Environment, systemd-Dienste, optional
# einen nginx-Reverse-Proxy mit TLS und startet die Anwendung im
# Einrichtungsassistenten (Port 8000, nur im lokalen Netz erreichbar).
# ---------------------------------------------------------------------------
set -Eeuo pipefail

APP_NAME="verzehr"
APP_DIR="${APP_DIR:-/opt/verzehr}"
APP_USER="${APP_USER:-$APP_NAME}"
REPO_URL="${REPO_URL:-}"
DOMAIN="${DOMAIN:-BEISPIEL.de}"
SERVICE_WEB="verzehr-web.service"
SERVICE_SCHED="verzehr-scheduler.service"

say()  { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m  %s\n' "$*" >&2; }
die()  { printf '\033[1;31mxx\033[0m  %s\n' "$*" >&2; exit 1; }

# Befehl als System-Benutzer ausfuehren. 'sudo' fehlt auf Minimal-Images,
# deshalb wird runuser (util-linux) bevorzugt.
as_app_user() {
  if command -v runuser >/dev/null 2>&1; then
    runuser -u "$APP_USER" -- "$@"
  else
    local quoted
    quoted=$(printf '%q ' "$@")
    su -s /bin/sh -c "$quoted" "$APP_USER"
  fi
}

trap 'die "Abbruch bei Zeile $LINENO"' ERR

# --- Voraussetzungen -------------------------------------------------------
[ "$(id -u)" -eq 0 ] || die "Bitte mit sudo ausfuehren."
for cmd in python3 systemctl; do
  command -v "$cmd" >/dev/null || die "$cmd fehlt. Bitte vorher installieren."
done
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
  || die "Python 3.11 oder neuer wird benoetigt."
python3 -c 'import venv, ensurepip' 2>/dev/null \
  || die "python3-venv fehlt. Debian/Ubuntu: apt install python3-venv"
[ -n "$REPO_URL" ] && { command -v git >/dev/null || die "git fehlt (wird fuer REPO_URL gebraucht)."; }

# --- System-Benutzer -------------------------------------------------------
if ! id -u "$APP_USER" >/dev/null 2>&1; then
  say "Lege System-Benutzer '$APP_USER' an"
  useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
else
  say "Benutzer '$APP_USER' existiert bereits"
fi

# --- Programmcode ----------------------------------------------------------
if [ -n "$REPO_URL" ]; then
  if [ -d "$APP_DIR" ] && [ -n "$(ls -A "$APP_DIR" 2>/dev/null)" ]; then
    # Nicht blind loeschen: bei einer Neuinstallation wird nahtlos aktualisiert.
    if [ -d "$APP_DIR/.git" ] || [ -f "$APP_DIR/wsgi.py" ]; then
      warn "$APP_DIR enthaelt bereits eine Installation."
      warn "  update.sh aktualisiert bestehende Installationen (bitte fuer Updates nehmen)."
      warn "  Zum Neuinstallieren:  sudo ./uninstall.sh --purge LOESCHEN"
      say "Nutze vorhandenen Quellcode in $APP_DIR"
    else
      die "$APP_DIR ist nicht leer und enthaelt keine Installation. Bitte erst leeren."
    fi
  else
    say "Hole Quellcode von $REPO_URL"
    install -d -o "$APP_USER" -g "$APP_USER" "$APP_DIR"
    git clone --depth 1 "$REPO_URL" "$APP_DIR"
  fi
elif [ -f "$APP_DIR/wsgi.py" ]; then
  say "Nutze vorhandenen Quellcode in $APP_DIR"
else
  say "Installiere aus dem aktuellen Verzeichnis nach $APP_DIR"
  install -d "$APP_DIR"
  tar --exclude='./.venv' --exclude='./.git' --exclude='./instance' \
      --exclude='./var' --exclude='__pycache__' --exclude='./.pytest_cache' \
      -cf - . | tar -xf - -C "$APP_DIR"
fi

cd "$APP_DIR"

# --- Verzeichnisse und Rechte ---------------------------------------------
say "Lege Datenverzeichnisse an"
install -d -o "$APP_USER" -g "$APP_USER" -m 0750 instance var/backups var/log

# --- Python-Umgebung -------------------------------------------------------
say "Erzeuge virtuelle Umgebung"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip wheel
"$APP_DIR/.venv/bin/pip" install --quiet -r requirements.txt
chown -R "$APP_USER:$APP_USER" "$APP_DIR/.venv"

# --- Konfiguration ---------------------------------------------------------
if [ ! -f .env ]; then
  say "Erzeuge .env aus .env.example"
  cp .env.example .env
  # Voreinstellungen fuer den Installationspfad
  sed -i "s#^APP_PORT=.*#APP_PORT=8000#" .env
  sed -i "s#^DATABASE_URL=.*#DATABASE_URL=sqlite:///${APP_DIR}/instance/payment.db#" .env
  sed -i "s#^APP_URL=.*#APP_URL=#" .env
  sed -i "s#^SECRET_KEY=.*##" .env
  # Der Web-Dienst laeuft mit mehreren Gunicorn-Workern. Damit dort nicht
  # jeder Worker einen eigenen Scheduler startet, laeuft die Tagesabrechnung
  # ausschliesslich im eigenen Dienst verzehr-scheduler.service.
  if grep -q '^SCHEDULER_ENABLED=' .env; then
    sed -i 's/^SCHEDULER_ENABLED=.*/SCHEDULER_ENABLED=false/' .env
  else
    printf '\n# Scheduler laeuft im eigenen systemd-Dienst, nicht im Web-Prozess.\nSCHEDULER_ENABLED=false\n' >> .env
  fi
fi
chown "$APP_USER:$APP_USER" .env
chmod 0640 .env

# --- systemd ---------------------------------------------------------------
say "Installiere systemd-Dienste"
install -m 0644 "systemd/${SERVICE_WEB}"   "/etc/systemd/system/${SERVICE_WEB}"
install -m 0644 "systemd/${SERVICE_SCHED}" "/etc/systemd/system/${SERVICE_SCHED}"
systemctl daemon-reload
systemctl enable "${SERVICE_WEB}" "${SERVICE_SCHED}"

# --- Datenbank und Schema --------------------------------------------------
say "Initialisiere Datenbank"
as_app_user "$APP_DIR/.venv/bin/python" manage.py initdb

# --- nginx (optional) ------------------------------------------------------
if command -v nginx >/dev/null 2>&1; then
  warn "nginx gefunden. Die Anwendung laeuft ab jetzt nur auf 127.0.0.1:8000."
  warn "Konfiguration uebernehmen (Domain in der Datei ersetzen):"
  warn "  sed -i 's/BEISPIEL.de/${DOMAIN}/g' ${APP_DIR}/systemd/verzehr.conf"
  warn "  cp ${APP_DIR}/systemd/verzehr.conf /etc/nginx/sites-available/verzehr.conf"
  warn "  ln -s ../sites-available/verzehr.conf /etc/nginx/sites-enabled/"
  warn "  nginx -t && systemctl reload nginx"
  warn "Danach in .env setzen:  TRUST_PROXY=1"
fi

# --- Start -----------------------------------------------------------------
say "Starte Dienst"
systemctl restart "${SERVICE_WEB}"
sleep 2
systemctl --no-pager --lines=0 status "${SERVICE_WEB}" || true

say "Fertig."
cat <<EOF

  Einrichtung abschliessen:
      http://localhost:8000/setup
    (bzw. http://SERVER-IP:8000/setup aus dem lokalen Netz)

  Nach der Einrichtung Firewall oeffnen (Beispiel ufw):
      ufw allow from 192.168.0.0/16 to any port 8000 proto tcp

  Status und Protokolle:
      systemctl status ${SERVICE_WEB} ${SERVICE_SCHED}
      journalctl -u ${SERVICE_WEB} -f

  Tagesabrechnung laeuft im Dienst '${SERVICE_SCHED}'.
  Fuer nginx/ TLS-Anleitung: ${APP_DIR}/README.md
EOF
