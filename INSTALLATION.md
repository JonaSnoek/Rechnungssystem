# Installation auf dem Server

Diese Anleitung führt Schritt für Schritt durch: Code auf den Server bringen,
`install.sh` ausführen, Einrichtung abschließen, Updates fahren.

Dauer ab geklonter Datei: etwa 5 Minuten, davon ca. 2 Minuten `pip install`.

---

## 1. Voraussetzungen prüfen

Auf dem Server (Debian 12 / Ubuntu 22.04 / Fedora als Beispiel):

```bash
cat /etc/os-release
python3 --version          # muss 3.11 oder neuer sein
```

Falls Python zu alt oder gar nicht vorhanden ist:

```bash
# Debian / Ubuntu
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git

# Fedora
sudo dnf install -y python3 python3-pip git
```

Optional, aber empfohlen: PostgreSQL statt SQLite, nginx als Reverse Proxy.
Beides ist **nicht** nötig, um zu starten.

```bash
sudo apt install -y nginx      # nur wenn eine eigene Domain gewünscht ist
```

---

## 2. Auf den Server kommen

### Variante A: per Git (empfohlen)

Funktioniert auf jedem Server mit `git`. So sind spätere Updates einfach.

```bash
sudo apt install -y git                        # falls noch nicht vorhanden
cd /opt
sudo git clone https://github.com/JonaSnoek/Rechnungssystem.git verzehr
cd verzehr
```

Das Repository ist **öffentlich lesbar**, es wird kein GitHub-Token gebraucht.

### Variante B: Dateien hochladen

Falls auf dem Server kein Git installiert werden darf. Von einem Rechner,
der das Repository hat:

```bash
# scp akzeptiert keine .gitignore - deshalb wird der Ordner vorher bereinigt.
rsync -av --exclude '.venv' --exclude '.git' --exclude 'instance' \
      --exclude '__pycache__' --exclude '.pytest_cache' \
      ./ /tmp/verzehr-transfer/

scp -r /tmp/verzehr-transfer root@SERVER-IP:/tmp/verzehr
```

Dann auf dem Server:

```bash
sudo mkdir -p /opt/verzehr
sudo cp -a /tmp/verzehr/. /opt/verzehr/
rm -rf /tmp/verzehr /tmp/verzehr-transfer
```

### Variante C: ZIP von GitHub

Zip herunterladen, auf den Server legen, auspacken:

```bash
sudo mkdir -p /opt/verzehr
sudo unzip Rechnungssystem-main.zip -d /tmp/vz
sudo cp -a /tmp/vz/Rechnungssystem-main/. /opt/verzehr/
rm -rf /tmp/vz
```

---

## 3. Ausführrechte setzen

Die Dateien aus einem Windows- oder ZIP-Transfer haben oft kein
Ausführbit. Deshalb **vor** dem Start:

```bash
cd /opt/verzehr
chmod +x install.sh update.sh uninstall.sh scripts/smoke.py
```

Ohne diesen Schritt meldet der Aufruf `Permission denied`.

Prüfen, ob es geklappt hat:

```bash
ls -l install.sh
#-rwxr-xr-x 1 root root 5412 ... install.sh      <- das x bei root genügt
```

---

## 4. `install.sh` ausführen

### Einfachster Fall

```bash
cd /opt/verzehr
sudo ./install.sh
```

Fertig. Das war alles.

### Mit eigener Domain (empfohlen für den Betrieb)

Damit `install.sh` das nginx-Protokoll schon mit dem richtigen Domainnamen
vorbereitet:

```bash
cd /opt/verzehr
sudo DOMAIN=verzehr.example.de ./install.sh
```

### Installation an einem anderen Ort

Standard ist `/opt/verzehr`. Für einen anderen Pfad:

```bash
sudo APP_DIR=/srv/verzehr ./install.sh
```

> Achtung: Änderst du `APP_DIR`, musst du anschließend in
> `systemd/verzehr-web.service` und `systemd/verzehr-scheduler.service` dieselbe
> Anpassung vornehmen (siehe Schritt 9).

### Vollständige Aufrufzeile mit allem:

```bash
cd /opt/verzehr
sudo DOMAIN=verzehr.example.de \
     APP_DIR=/opt/verzehr \
     APP_USER=verzehr \
     ./install.sh
```

### Alternative ohne `./`

Falls `./install.sh` nicht klappt, ist es der übliche Aufruf:

```bash
cd /opt/verzehr
sudo bash install.sh
```

Das funktioniert auch, wenn das Ausführbit fehlt. `sudo sh install.sh` ist
**nicht** empfehlenswert – das Skript nutzt Bash-Syntax.

### Was `install.sh` macht

| Schritt | Was passiert |
| --- | --- |
| 1 | Prüft `root`, Python ≥ 3.11, `python3-venv`, `systemctl` |
| 2 | Legt den System-Benutzer `verzehr` an (ohne Login-Shell) |
| 3 | Holt den Quellcode, falls `REPO_URL` gesetzt ist |
| 4 | Legt `instance/`, `var/backups/`, `var/log/` an |
| 5 | Erzeugt `.venv` und installiert `requirements.txt` |
| 6 | Erzeugt `.env` aus `.env.example`, Rechte `0640` |
| 7 | Installiert `verzehr-web.service` und `verzehr-scheduler.service`, aktiviert sie |
| 8 | Initialisiert Datenbank und Migrationen |
| 9 | Startet den Web-Dienst und zeigt den Status |

Das Skript bricht bei jedem Fehler ab und nennt die Zeile. Läuft es zweimal
auf einer Installation, erkennt es das und verweist auf `update.sh`, statt
versehentlich Daten zu überschreiben.

### Erwartete Ausgabe am Ende

```
==> Fertig.

  Einrichtung abschliessen:
      http://localhost:8000/setup
    (bzw. http://SERVER-IP:8000/setup aus dem lokalen Netz)
  ...
```

---

## 5. Erreichbar machen

Direkt nach der Installation hört Gunicorn **nur** auf `127.0.0.1:8000`.
Das ist Absicht – die Anwendung ist so nicht von außen erreichbar.

Für die Ersteinrichtung aus dem LAN (SSH-Tunnel ist die sicherste Variante):

```bash
# auf deinem Rechner
ssh -L 8000:127.0.0.1:8000 root@SERVER-IP
# dann im Browser: http://localhost:8000/setup
```

Oder temporär im LAN, mit Firewall-Regel:

```bash
sudo sed -i 's/^APP_HOST=.*/APP_HOST=0.0.0.0/' /opt/verzehr/.env
sudo systemctl restart verzehr-web

sudo ufw allow from 192.168.0.0/16 to any port 8000 proto tcp
# später wieder entfernen:
sudo ufw delete allow from 192.168.0.0/16 to any port 8000 proto tcp
```

Prüfen, ob der Dienst läuft:

```bash
curl -I http://127.0.0.1:8000/login
systemctl status verzehr-web --no-pager
```

---

## 6. Einrichtung abschließen

```
http://localhost:8000/setup
```

Sechs Schritte:

1. **Datenbank** – Verbindung prüfen. Standard ist SQLite, passt so.
2. **Administrator** – Benutzername, E-Mail, Passwort (mind. 12 Zeichen).
3. **PayPal** – dein `paypal.me`-Benutzername, z. B. `JonaSnoek1`.
4. **E-Mail** – SMTP-Server, Port, Verschlüsselung, Absenderadresse.
   Das Passwort wird in `instance/secrets.env` abgelegt, nicht in `.env`.
5. **Abrechnung** – Uhrzeit für die tägliche Abrechnung, Zeitzone, Nachholen.
6. **Fertig** – abschließen, danach Anmeldung mit dem Admin-Konto.

Danach ein **Test**: eine Person anlegen, ein Produkt anlegen, einen Verzehr
buchen, in der Personenliste auf **Rechnung senden** klicken. Die Rechnung
sollte als E-Mail ankommen.

---

## 7. HTTPS mit eigener Domain

### 7.1 DNS

Einen A-Record auf die Server-IP setzen, z. B. `verzehr.example.de`.

### 7.2 Zertifikat

Port 80 muss dafür kurz frei sein –nginx ist währenddessen nicht aktiv:

```bash
sudo apt install -y certbot
sudo certbot certonly --standalone -d verzehr.example.de -d www.verzehr.example.de
```

### 7.3 nginx-Konfiguration aktivieren

```bash
sudo sed -i 's/BEISPIEL.de/verzehr.example.de/g' /opt/verzehr/systemd/verzehr.conf

sudo cp /opt/verzehr/systemd/verzehr.conf      /etc/nginx/sites-available/verzehr.conf
sudo cp /opt/verzehr/systemd/verzehr-http.conf /etc/nginx/sites-available/verzehr-http.conf
sudo ln -s ../sites-available/verzehr.conf       /etc/nginx/sites-enabled/
sudo ln -s ../sites-available/verzehr-http.conf /etc/nginx/sites-enabled/

sudo nginx -t && sudo systemctl reload nginx
```

### 7.4 Proxy dem Programm mitteilen

Damit die Anwendung das echte Protokoll und die Client-IP sieht:

```bash
sudo sh -c 'echo "TRUST_PROXY=1" >> /opt/verzehr/.env'
sudo systemctl restart verzehr-web
```

### 7.5 Fertig

```
https://verzehr.example.de/login
```

Port 8000 wird nicht mehr ins Internet freigegeben:

```bash
sudo ufw delete allow 8000/tcp 2>/dev/null || true
```

---

## 8. Updates

```bash
cd /opt/verzehr
sudo ./update.sh
```

`update.sh` macht der Reihe nach:

1. **Backup** anlegen – bricht ab, wenn das nicht klappt
2. Scheduler und Web-Dienst stoppen
3. `git pull` (bzw. Dateien ersetzen)
4. Abhängigkeiten aktualisieren
5. offene Migrationen ausführen
6. beide Dienste neu starten

Bei einem Server ohne Git (Variante B oder C) zuerst die neuen Dateien
übertragen, dann `sudo ./update.sh`.

Rollback bei einem fehlgeschlagenen Update:

```bash
sudo systemctl stop verzehr-web verzehr-scheduler
cd /opt/verzehr
sudo git log --oneline -5          # letzte gute Version finden
sudo git checkout <KOMMIT>
sudo .venv/bin/pip install -r requirements.txt
sudo .venv/bin/python manage.py migrate
sudo systemctl start verzehr-web verzehr-scheduler
```

---

## 9. `APP_DIR` anpassen

Nur nötig, wenn du nicht unter `/opt/verzehr` installiert hast. In **beiden**
Unit-Dateien `/opt/verzehr` durch deinen Pfad ersetzen:

```bash
sudo sed -i 's#/opt/verzehr#/srv/verzehr#g' /opt/verzehr/systemd/*.service
sudo cp /opt/verzehr/systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart verzehr-web verzehr-scheduler
```

---

## 10. Deinstallation

Dienste und Programmcode entfernen, **Daten behalten**:

```bash
cd /opt/verzehr
sudo ./uninstall.sh
```

Datenbank, Secrets und Backups endgültig löschen – verlangt die
Eingabe `LOESCHEN`:

```bash
sudo ./uninstall.sh --purge LOESCHEN
```

Bei einem nicht-interaktiven Aufruf:

```bash
sudo ./uninstall.sh --purge LOESCHEN
```

Ohne das Wort `LOESCHEN` passiert beim `--purge` nichts.

---

## 11. Alltägliche Befehle

```bash
# Status
sudo systemctl status verzehr-web verzehr-scheduler
sudo journalctl -u verzehr-web -f          # Protokoll live
sudo journalctl -u verzehr-scheduler -f

# Anwendung
cd /opt/verzehr
sudo -u verzehr .venv/bin/python manage.py status
sudo -u verzehr .venv/bin/python manage.py check

# Backup
sudo -u verzehr .venv/bin/python manage.py backup

# Passwort zurücksetzen (wenn der Login vergessen wurde)
sudo -u verzehr .venv/bin/python manage.py create-admin NAME EMAIL
```

---

## 12. Wenn etwas nicht klappt

### `Permission denied` beim Start von `install.sh`

```bash
cd /opt/verzehr
chmod +x install.sh
sudo ./install.sh
```

### `python3-venv fehlt` oder `No module named venv`

```bash
sudo apt install -y python3-venv
```

### `Failed to connect to bus` bei systemctl

`install.sh` braucht systemd. Auf einem Container ohne systemd nicht
verwendbar – dann manuell starten (siehe Abschnitt 13).

### `Address already in use` / Port 8000 belegt

```bash
sudo ss -tlnp | grep 8000
# anderen Port setzen:
sudo sed -i 's/^APP_PORT=.*/APP_PORT=8080/' /opt/verzehr/.env
sudo sed -i 's/127.0.0.1:8000/127.0.0.1:8080/' /etc/systemd/system/verzehr-web.service
sudo systemctl daemon-reload && sudo systemctl restart verzehr-web
```

### E-Mails kommen nicht an

```bash
sudo -u verzehr .venv/bin/python manage.py check
journalctl -u verzehr-scheduler -n 100 --no-pager
```

Häufigste Ursachen: SMTP-Host falsch, Absenderadresse wird vom Provider
abgelehnt, oder Port 587 ist in der Firewall des Providers dicht.
Bei Gmail oder GMX braucht es ein App-Passwort.

### Passwort vergessen

```bash
cd /opt/verzehr
sudo -u verzehr .venv/bin/python manage.py create-admin NAME EMAIL --password 'NeuesPasswort!2026'
```

### Rauchtest

Prüft jede Seite, ohne etwas zu verändern oder zu versenden:

```bash
sudo -u verzehr .venv/bin/python scripts/smoke.py
```

Ausgabe mit `keine Fehler` bedeutet: alle Routen antworten.

---

## 13. Betrieb ohne systemd (Docker, LXC, Notfall)

```bash
cd /opt/verzehr

# Web
sudo -u verzehr .venv/bin/gunicorn --workers 2 --bind 127.0.0.1:8000 wsgi:app

# Scheduler (eigenes Terminal, bleibt laufen)
sudo -u verzehr .venv/bin/python scheduler.py
```

Dabei `SCHEDULER_ENABLED=false` in `.env` setzen, sonst startet **jeder**
Gunicorn-Worker einen eigenen Scheduler.

---

## Kurzform für Erfahrene

```bash
sudo apt install -y python3 python3-venv git
cd /opt && sudo git clone https://github.com/JonaSnoek/Rechnungssystem.git verzehr
cd verzehr && sudo chmod +x install.sh && sudo DOMAIN=example.de ./install.sh
ssh -L 8000:127.0.0.1:8000 root@SERVER-IP     # dann http://localhost:8000/setup
```
