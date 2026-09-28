#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Verzehrabrechnung - Installation
#
#   sudo ./install.sh
#
# Legt an: System-Benutzer, virtuelles Environment, systemd-Dienste und startet
# die Anwendung im Einrichtungsassistenten. Standardmaessig ist sie danach
# unter der IP-Adresse des Servers erreichbar:
#
#     http://<SERVER-IP>:8000/setup
#
# Mit BIND_ADDR=127.0.0.1 wird nur lokal gebunden (Pflicht, sobald nginx
# davorsteht). Mit OPEN_FIREWALL=1 wird der Port zusaetzlich in der Firewall
# freigegeben.
# ---------------------------------------------------------------------------
set -Eeuo pipefail

APP_NAME="verzehr"
APP_DIR="${APP_DIR:-/opt/verzehr}"
APP_USER="${APP_USER:-$APP_NAME}"
REPO_URL="${REPO_URL:-}"
DOMAIN="${DOMAIN:-BEISPIEL.de}"
# Erreichbarkeit: 0.0.0.0 = unter der Server-IP erreichbar (Standard),
#                  127.0.0.1 = nur lokal, fuer den Betrieb hinter nginx.
BIND_ADDR="${BIND_ADDR:-0.0.0.0}"
BIND_PORT="${BIND_PORT:-8000}"
OPEN_FIREWALL="${OPEN_FIREWALL:-0}"
SERVICE_WEB="verzehr-web.service"
SERVICE_SCHED="verzehr-scheduler.service"

case "$BIND_ADDR" in
  0.0.0.0|127.0.0.1) ;;
  *) die "BIND_ADDR muss 0.0.0.0 oder 127.0.0.1 sein, nicht '$BIND_ADDR'." ;;
esac
[[ "$BIND_PORT" =~ ^[0-9]+$ ]] && [ "$BIND_PORT" -ge 1 ] && [ "$BIND_PORT" -le 65535 ] \
  || die "BIND_PORT muss eine Zahl zwischen 1 und 65535 sein, nicht '$BIND_PORT'."

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

# Alle IPv4-Adressen dieses Servers, damit am Ende die echten URLs stehen.
server_ips() {
  { hostname -I 2>/dev/null || ip -4 -o addr show scope global 2>/dev/null | awk '{print $4}'; } \
    | tr ' ' '\n' | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' || true
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
  sed -i "s#^APP_HOST=.*#APP_HOST=${BIND_ADDR}#" .env
  sed -i "s#^APP_PORT=.*#APP_PORT=${BIND_PORT}#" .env
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

# Gunicorn nimmt seine Bind-Adresse aus ExecStart der Unit-Datei, nicht aus
# .env. Deshalb wird sie hier passend zu BIND_ADDR gesetzt.
sed -i "s#^\( *\)--bind [^ ]*#\1--bind ${BIND_ADDR}:${BIND_PORT}#" \
       "/etc/systemd/system/${SERVICE_WEB}"
if [ "$BIND_ADDR" = "0.0.0.0" ]; then
  say "Web-Dienst bindet auf ${BIND_ADDR}:${BIND_PORT} (erreichbar unter der Server-IP)"
else
  say "Web-Dienst bindet auf ${BIND_ADDR}:${BIND_PORT} (nur lokal, nginx uebernimmt)"
fi

systemctl daemon-reload
systemctl enable "${SERVICE_WEB}" "${SERVICE_SCHED}"

# --- Datenbank und Schema --------------------------------------------------
say "Initialisiere Datenbank"
as_app_user "$APP_DIR/.venv/bin/python" manage.py initdb

# --- nginx (optional) ------------------------------------------------------
if command -v nginx >/dev/null 2>&1; then
  warn "nginx gefunden. Hinweise zur Domain-Uebernahme in ${APP_DIR}/INSTALLATION.md"
  if [ "$BIND_ADDR" = "0.0.0.0" ]; then
    warn "ACHTUNG: nginx ist installiert, der Dienst haengt aber weiterhin direkt"
    warn "  unter ${BIND_ADDR}:${BIND_PORT} im Netz. Nach der nginx-Uebernahme auf"
    warn "  Loopback zurueck:  sudo env BIND_ADDR=127.0.0.1 ./install.sh"
    warn "  Danach in .env:   TRUST_PROXY=1"
  fi
fi

# --- Firewall ---------------------------------------------------------------
if [ "$BIND_ADDR" = "0.0.0.0" ]; then
  FW_HANDLED=0
  if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q '^Status: active'; then
    if [ "$OPEN_FIREWALL" = "1" ]; then
      say "Oeffne Port ${BIND_PORT} in ufw"
      ufw allow "${BIND_PORT}/tcp" >/dev/null
    else
      warn "ufw ist aktiv und blockiert Port ${BIND_PORT} vermutlich."
      warn "  Freigeben:  sudo ufw allow ${BIND_PORT}/tcp"
      warn "  oder bei der Installation:  sudo env OPEN_FIREWALL=1 ./install.sh"
    fi
    FW_HANDLED=1
  fi
  if command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
    if [ "$OPEN_FIREWALL" = "1" ]; then
      say "Oeffne Port ${BIND_PORT} in firewalld"
      firewall-cmd --permanent --add-port="${BIND_PORT}/tcp" >/dev/null
      firewall-cmd --reload >/dev/null
    else
      warn "firewalld ist aktiv. Freigeben mit:"
      warn "  sudo firewall-cmd --permanent --add-port=${BIND_PORT}/tcp && sudo firewall-cmd --reload"
    fi
    FW_HANDLED=1
  fi
  if [ "$FW_HANDLED" = "0" ]; then
    warn "Keine aktive Firewall gefunden - Port ${BIND_PORT} ist offen erreichbar."
  fi
fi

# --- Start -----------------------------------------------------------------
say "Starte Dienst"
systemctl restart "${SERVICE_WEB}"
sleep 2
systemctl --no-pager --lines=0 status "${SERVICE_WEB}" || true

# Erst nach dem Start pruefen, ob der Dienst wirklich antwortet.
if curl -sf -o /dev/null "http://127.0.0.1:${BIND_PORT}/login" 2>/dev/null \
   || wget -q -O /dev/null "http://127.0.0.1:${BIND_PORT}/login" 2>/dev/null; then
  say "Dienst antwortet auf Port ${BIND_PORT}"
else
  warn "Der Dienst antwortet noch nicht auf Port ${BIND_PORT}."
  warn "  systemctl status ${SERVICE_WEB}"
  warn "  journalctl -u ${SERVICE_WEB} -n 50 --no-pager"
fi

# --- Abschluss -------------------------------------------------------------
IPS="$(server_ips | sort -u)"
say "Fertig."
echo
echo "  Einrichtung abschliessen unter:"
if [ "$BIND_ADDR" = "0.0.0.0" ]; then
  if [ -n "$IPS" ]; then
    while IFS= read -r ip; do
      echo "      http://${ip}:${BIND_PORT}/setup"
    done <<< "$IPS"
  fi
  echo "      http://localhost:${BIND_PORT}/setup   (nur auf dem Server selbst)"
else
  echo "      http://localhost:${BIND_PORT}/setup   (nur lokal gebunden)"
  echo "      Fuer Zugriff von aussen:  sudo env BIND_ADDR=0.0.0.0 ./install.sh"
  echo "      oder einen SSH-Tunnel nutzen:"
  echo "          ssh -L ${BIND_PORT}:127.0.0.1:${BIND_PORT} root@SERVER-IP"
fi
echo
echo "  Status und Protokolle:"
echo "      systemctl status ${SERVICE_WEB} ${SERVICE_SCHED}"
echo "      journalctl -u ${SERVICE_WEB} -f"
echo
echo "  Tagesabrechnung laeuft im Dienst '${SERVICE_SCHED}'."
echo "  Anleitung: ${APP_DIR}/INSTALLATION.md"
if [ "$BIND_ADDR" = "0.0.0.0" ]; then
  echo "  HTTPS mit eigener Domain: Abschnitt 7 in ${APP_DIR}/INSTALLATION.md"
fi
echo
