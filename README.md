# Verzehrabrechnung

Selbst gehostete Abrechnung für Verzehr und Getränke: Buchungen erfassen,
offene Beträge je Person abrechnen, Rechnungen per E-Mail mit PayPal.Me-Link
versenden und Zahlungen manuell verfolgen.

* Rechnungen enthalten **genau die Buchungen**, die beim Erstellen der Rechnung
  offen waren – keine späteren Zugänge, keine Doppelabrechnung.
* Der offene Betrag ist die Summe aller Buchungen mit Status `OFFEN`. Eine
  fehlgeschlagene Rechnung lässt ihre Buchungen offen, damit sie erneut
  versendet werden können.
* Nichts wird nach Zeit gelöscht. Verrechnete Buchungen bleiben als Historie
  erhalten, Rechnungen lassen sich stornieren (die Buchungen werden dann wieder
  offen).
* Geld wird ausschließlich als ganzzahliger Betrag in Cent gerechnet.

---

## Inhalt

1. [Schnellstart](#schnellstart)
2. [Installation auf dem Server](INSTALLATION.md) — ausführliche Anleitung
3. [Abrechnungslogik](#abrechnungslogik)
4. [Betrieb ohne Root](#betrieb-ohne-root)
5. [Konfiguration](#konfiguration)
6. [nginx und TLS](#nginx-und-tls)
7. [Dienste](#dienste)
8. [Wartung und Backups](#wartung-und-backups)
9. [Update und Deinstallation](#update-und-deinstallation)
10. [Entwicklung und Tests](#entwicklung-und-tests)
11. [Projektstruktur](#projektstruktur)
12. [Sicherheit](#sicherheit)

---

## Schnellstart

Voraussetzungen: Linux mit systemd, Python 3.11+, PostgreSQL optional
(Standard ist SQLite, es muss nichts installiert werden).

```bash
git clone https://github.com/JonaSnoek/Rechnungssystem.git verzehr && cd verzehr
sudo chmod +x install.sh          # bei Windows-/ZIP-Transfer nötig
sudo ./install.sh
```

`install.sh` legt System-Benutzer, virtuelle Umgebung, Datenbank und beide
systemd-Dienste an. Die Anwendung ist danach **unter der IP des Servers**
erreichbar; das Skript zeigt die passenden URLs zum Schluss an:

```
http://<server-ip>:8000/setup
```

Ist der Port durch die Firewall blockiert, hilft `sudo env OPEN_FIREWALL=1 ./install.sh`.

Der Assistent hat sechs Schritte: Datenbank, Administrator, PayPal.Me,
SMTP-Mailserver, Abrechnungszeit, fertig. PayPal.Me und SMTP lassen sich auch
später unter *Einstellungen* ändern.

**Ausführliche Anleitung mit allen Varianten (Git, Upload, ZIP, eigene
Domain, Fehlersuche): [INSTALLATION.md](INSTALLATION.md)**

Für eine reine Testinstallation ohne systemd:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
SCHEDULER_ENABLED=true .venv/bin/python wsgi.py     # http://localhost:8000
```

---

## Abrechnungslogik

### Offener Betrag

```
offener Betrag = Summe aller Buchungen mit Status OFFEN
```

Eine Buchung ist `OFFEN`, solange sie keiner **erfolgreich versendeten**
Rechnung zugeordnet ist. Eine fehlgeschlagene Rechnung zählt nicht als
Zahlung: ihre Buchungen bleiben `OFFEN` und behalten die `invoice_id`, damit
derselbe Vorgang erneut versendet werden kann.

### Ein Billing-Lauf

1. Für jede Person mit offenen Buchungen werden alle Rechnungen gesucht, die
   noch nie zugestellt wurden – auch aus früheren Tagen. Sie werden
   chronologisch erneut versendet. Erst danach geht es weiter.
2. Bleiben danach noch unzugeordnete offene Buchungen übrig, entsteht **eine
   neue Rechnung** daraus.
3. Schlägt der Versand einer offenen Rechnung fehl, bricht der Lauf für diese
   Person ab. Es entsteht keine weitere Rechnung, solange der Versand
   kaputt ist.

Es gibt keine Uhrzeitregel und keinen Tageswechsel. Die konfigurierte
Abrechnungszeit (Vorgabe 17:00) ist nur der Auslöser des Schedulers. Wann
genau eine Rechnung entsteht, entscheidet ausschließlich: *Was ist offen?*

### Mehrere Rechnungen am selben Tag

Erlaubt und vorgesehen. Pro Person und Tag läuft die Nummerierung über das
Feld `sequence`; die Rechnungsnummer lautet `RE-<Jahr><Monat><Tag>-<Nr.>`.
Sequenzen werden **nie wiederverwendet**, auch nicht nach dem Löschen einer
Rechnung.

### Storno

Storniert wird über `Invoice.status = STORNIERT`. Dabei werden die
zugeordneten Buchungen auf `OFFEN` gesetzt und `invoice_id` wird geleert. Die
Rechnung selbst bleibt als Historie stehen, inklusive Positionen. Beim
nächsten Lauf kann sie als neue Rechnung erstellt werden – der Abbuchbetrag
bleibt identisch.

### Guthabenkonto

Jede Person hat genau ein Konto. Der Kontostand ist **Einzahlungen minus
Verzehr**: positiv ist Guthaben, negativ eine Schuld.

| Schritt | Wirkung auf das Konto |
| --- | --- |
| Verzehr erfassen | belastet **sofort** beim Speichern |
| Einzahlung erfassen | günstigt das Konto |
| Rechnung senden | **keine** Wirkung, nur Ausweis der Verrechnung |

Das ist der entscheidende Punkt: Eine Buchung wird genau einmal belastet, und
zwar dann, wenn sie entsteht. Die Rechnung belastet nichts erneut, sondern weist
nur aus, welcher Teil des Verzehrs durch damaliges Guthaben gedeckt war
(`credit_applied_cents`) und wie viel zu zahlen bleibt (`amount_due_cents`). Wird
eine Rechnung vollständig durch Guthaben gedeckt, entsteht **kein PayPal-Link**.

Damit das auch bei mehreren Rechnungen pro Tag stimmt, speichert jede Buchung
das Guthaben, das sie bei der Erfassung verbraucht hat. Die Rechnung summiert
diese Werte, statt das Konto erneut anzufassen.

### Kontobewegungen werden nur angehängt

`ledger_entries` wird nie geändert oder gelöscht. Eine falsche Buchung wird
durch eine Gegenbewegung (`KORREKTUR`) ausgeglichen, die über `reverses_entry_id`
auf die ursprüngliche Bewegung zeigt. Beide bleiben sichtbar.

Eindeutigkeit ist auf Datenbankebene gesichert: `consumption_id`, `deposit_id`
und `reverses_entry_id` sind jeweils `UNIQUE`. Eine doppelte Buchung kann deshalb
nicht entstehen, egal wie oft ein Ablauf wiederholt wird.

### E-Mail getrennt von der Einzahlung

Die Einzahlung wird **vor** dem Mailversand gespeichert. Schlägt der Versand
fehl, ist das Geld trotzdem gebucht; nur `deposits.email_status` steht dann auf
`FEHLGESCHLAGEN`. Ein erneuter Versuch (`/personen/<id>/konto`,
Button *Erneut senden*) ändert den Kontostand nicht.

Ein PayPal-Link gilt nie als Zahlungsbestätigung: Eingezahltes wird als
Einzahlung von Hand erfasst.

### Altdaten

Bestandsdaten werden beim ersten Start nach dem Schema-Wechsel automatisch
übernommen (`ensure_accounts`): jede Person bekommt ein Konto, jede nicht
stornierte Buchung eine Bewegung. Der Vorgang ist wiederholbar und erfindet
**keine** historischen Einzahlungen – Zahlungen, die nie erfasst wurden, bleiben
unsichtbar. Dafür gibt es auf der Kontoseite *Anfangsbestand korrigieren*, das
den Zielwert als sichtbare Korrekturbuchung setzt.

### Zahlungsstatus getrennt vom Versandstatus

| Feld | Werte |
| --- | --- |
| `Invoice.status` | `OFFEN` (erstellt), `VERSENDT`, `FEHLGESCHLAGEN`, `STORNIERT` |
| `Invoice.payment_status` | `OFFEN`, `ZAHLUNG_ANGEFORDERT`, `BEZAHLT`, `STORNIERT` |
| `Consumption.status` | `OFFEN`, `ABGERECHNET`, `STORNIERT` |

Zurücksetzen der Zahlung setzt `payment_status` auf `ZAHLUNG_ANGEFORDERT` und
löscht `paid_at` – der Versandstatus bleibt unberührt.

### Verpasste Tage

Läuft der Rechner um 17:00 nicht, holt der Scheduler beim nächsten Start
nach. `scheduler_state.last_billed_period` merkt sich den letzten abgerechneten
Tag; nachgeholt wird ein Zeitraum pro Tick, damit sich nicht alles staut.

---

## Betrieb ohne Root

Für den Betrieb ohne systemd und ohne nginx:

```bash
.venv/bin/gunicorn --workers 2 --bind 127.0.0.1:8000 wsgi:app
.venv/bin/python scheduler.py
```

Bei mehreren Gunicorn-Workern **muss** `SCHEDULER_ENABLED=false` gesetzt
werden, sonst startet jeder Worker einen eigenen Scheduler. (Die Datenbank
sichert das zusätzlich über ein Heartbeat-Lock ab, aber ein einziger
Scheduler-Prozess ist eindeutiger.)

`install.sh` bindet standardmäßig auf `0.0.0.0:8000`, damit die Anwendung
unter der IP des Servers erreichbar ist. Umgestellt wird das über Variablen,
die Unit-Datei und `.env` gemeinsam setzen:

```bash
sudo env BIND_ADDR=127.0.0.1 ./install.sh    # nur lokal, Pflicht hinter nginx
sudo env BIND_PORT=8080 ./install.sh         # anderer Port
sudo env OPEN_FIREWALL=1 ./install.sh        # Port in ufw/firewalld öffnen
```

`sudo env VAR=…` statt `sudo VAR=…`, weil sudo Variablen sonst nicht
durchreicht. Für alles darüber hinaus bitte TLS vorschalten, siehe
[nginx und TLS](#nginx-und-tls).

---

## Konfiguration

`.env` im Projektverzeichnis, Muster in `.env.example`. Vorrang:

1. echte Umgebungsvariablen (systemd, Shell)
2. `.env`
3. `instance/secrets.env` (im Assistenten gewählte Werte, z. B. SMTP-Passwort)
4. built-in Vorgaben

`SECRET_KEY`, `SMTP_PASSWORD` und `DB_ENCRYPTION_KEY` liegen in
`instance/secrets.env` (Rechte `0600`) und niemals im Repository.

### Reihenfolge für `DATABASE_URL`

1. echte Umgebungsvariable – hat Vorrang und lässt sich im Assistenten
   deshalb **nicht** ändern
2. `instance/secrets.env` – im Assistenten gewählt, überlebt den Neustart
3. `.env`
4. Vorgabe `sqlite:///instance/payment.db`

Schritt 1 des Assistenten erlaubt das Umschalten der Datenbank, solange die
Verbindung nicht per Umgebungsvariable festgenagelt ist. Beim Speichern wird
die Verbindung getestet, das Schema angelegt und die neue URL in
`instance/secrets.env` geschrieben. Passwörter in der URL werden nie
angezeigt.

### PostgreSQL

```bash
sudo -u postgres psql -c "CREATE USER verzehr WITH PASSWORD 'geheim';"
sudo -u postgres psql -c "CREATE DATABASE verzehr OWNER verzehr;"
```

```bash
DATABASE_URL=postgresql+psycopg2://verzehr:geheim@127.0.0.1:5432/verzehr
```

Vor einem Wechsel unbedingt ein Backup anlegen (siehe
[Update und Deinstallation](#update-und-deinstallation)).

---

## nginx und TLS

Vorlagen liegen in `systemd/verzehr.conf` (HTTPS) und
`systemd/verzehr-http.conf` (HTTP-Only). `BEISPIEL.de` durch die eigene Domain
ersetzen, dann:

```bash
sudo cp systemd/verzehr.conf      /etc/nginx/sites-available/verzehr.conf
sudo cp systemd/verzehr-http.conf /etc/nginx/sites-available/verzehr-http.conf
sudo ln -s ../sites-available/verzehr.conf       /etc/nginx/sites-enabled/
sudo ln -s ../sites-available/verzehr-http.conf /etc/nginx/sites-enabled/
```

Die Anwendung lauscht nach der Installation auf `0.0.0.0:8000` und ist damit
unter der IP des Servers erreichbar. Sobald nginx übernimmt, auf Loopback
zurückbinden, damit der Port nicht zusätzlich offen im Netz hängt:

```bash
cd /opt/verzehr && sudo env BIND_ADDR=127.0.0.1 ./install.sh
```

Aktuellen Zertifikatspfad eintragen und nginx neu laden:

```bash
sudo nginx -t && sudo systemctl reload nginx
```

Damit `X-Forwarded-Proto` (HTTPS) und die Client-IP korrekt ankommen, muss in
`.env` stehen:

```
TRUST_PROXY=1
```

---

## Dienste

| Dienst | Aufgabe |
| --- | --- |
| `verzehr-web.service` | Gunicorn, HTTP-Oberfläche |
| `verzehr-scheduler.service` | Tagesabrechnung, genau eine Instanz |

```bash
sudo systemctl status verzehr-web verzehr-scheduler
sudo journalctl -u verzehr-web -f
sudo systemctl restart verzehr-web        # Konfigurationsänderungen
```

Einstellungen wie Zeitzone oder Abrechnungszeit werden im laufenden Betrieb
übernommen; ein Neustart ist dafür nicht nötig.

---

## Wartung und Backups

```bash
.venv/bin/python manage.py status      # Konfiguration und Datenbank
.venv/bin/python manage.py check       # inkl. Test-Backup und SMTP-Test
.venv/bin/python manage.py backup      # JSON-Backup
.venv/bin/python manage.py create-admin NAME EMAIL   # Zugang anlegen/zurücksetzen
.venv/bin/python manage.py change-password NAME
.venv/bin/python manage.py secret show
```

`create-admin` und `change-password` fragen das Passwort interaktiv ab, wenn
kein `--password` angegeben wird.

Zusätzlich zur JSON-Sicherung die Datenbankdatei kopieren:

```bash
sudo systemctl stop verzehr-web
sudo -u verzehr cp instance/payment.db "instance/backup-$(date +%F).db"
sudo systemctl start verzehr-web
```

Daten liegen unter `instance/` (Datenbank, `secrets.env`, `backups/`) und
`var/backups/`. Beides gehört nicht ins Git-Repository.

---

## Update und Deinstallation

```bash
sudo ./update.sh
```

Legt zuerst ein Backup an, aktualisiert die Abhängigkeiten, führt offene
Migrationen aus und startet beide Dienste neu. Ohne Backup im laufenden
Betrieb bricht das Skript ab.

```bash
sudo ./uninstall.sh              # Dienste und Code weg, Daten behalten
sudo ./uninstall.sh --purge LOESCHEN   # inklusive Datenbank und Backups
```

`--purge` löscht unwiderruflich und verlangt deshalb die Eingabe `LOESCHEN`.

---

## Entwicklung und Tests

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest -q
```

Jeder Test bekommt eine frische SQLite-Datenbank im Temp-Verzeichnis, einen
eigenen Secret Key und einen Mailer, der keinen echten SMTP-Server braucht.
Es wird nie ein echter Mailversand ausgelöst.

| Datei | Inhalt |
| --- | --- |
| `tests/conftest.py` | Fixtures: App, Session, Login, Fake-Mailer, Buchungs-Helfer |
| `tests/test_billing_spec.py` | die Abrechnungsregeln, Abschnitt für Abschnitt |
| `tests/test_web.py` | HTTP-Oberfläche, Setup, CRUD, CSRF, Rechte, Fehlerseiten |
| `tests/test_cli.py` | `manage.py` als Subprozess, inklusive Exit-Codes |

Ein Mailer-Ausfall lässt sich gezielt simulieren:

```python
def test_retry(book, bill, mailer, invoices):
    mailer.fail = True
    bill()          # Versand scheitert, Buchungen bleiben OFFEN
    mailer.fail = False
    bill()          # erneuter Versand derselben Rechnung
    assert invoices()[0].send_attempts == 2
```

### Rauchtest nach der Installation

```bash
.venv/bin/python scripts/smoke.py
```

Startet die Anwendung im Speicher, ruft jede GET-Route auf und meldet
Fehlerseiten. Es wird nichts versendet und nichts an der Datenbank geändert.
Mit `SMOKE_USE_REAL_DB=1` prüft er die installierte Datenbank.

---

## Projektstruktur

```
app/
  __init__.py            App-Factory, Jinja-Globals, CSRF, Fehlerseiten
  config.py              .env / Umgebung / Vorgaben
  db.py                  Engine, Session, SQLite-Tuning
  models.py              ORM inkl. Status-Enums
  migrations.py          SQL-Migrationen
  scheduler.py           minütlicher Tick, Nachholung, Lock
  security.py            Passwörter, CSRF, Session, Audit-Log
  mailer.py              SMTP-Versand
  views/                 HTTP-Endpunkte je Bereich
  services/              Fachlogik (Abrechnung, Backup, Einstellungen)
  templates/             Jinja-Templates
  static/                CSS und JavaScript
migrations/              versionierte SQL-Dateien
systemd/                 Unit-Dateien und nginx-Vorlagen
scripts/smoke.py         Rauchtest aller GET-Routen
tests/                   pytest-Suite
instance/                Datenbank, Secrets, Backups (nicht im Git)
```

---

## Sicherheit

* Passwörter als PBKDF2-SHA256 mit Salt, Vergleich in konstanter Zeit.
* Passwortregeln und Rate-Limit auf dem Login; nginx begrenzt `/login`
  zusätzlich.
* CSRF-Token auf jedem Formular, `SameSite=Lax`-Session-Cookie, nur über
  HTTPS, `HttpOnly`, Fingerabdruck aus User-Agent.
* Sitzung läuft nach vier Stunden ab.
* Alle Rechnungs- und Zahlungsänderungen landen im Audit-Log
  (*System → Protokolle*).
* SMTP-Passwort und `SECRET_KEY` in `instance/secrets.env` mit `0600`.
* systemd-Unit läuft mit `ProtectSystem=strict`; beschreibbar sind nur
  `instance/` und `var/`.
* Ausgaben werden in Jinja automatisch escaped; `{produkte_html}` wird
  bewusst als bereits HTML-kennzeichneten Block eingefügt.

Vor dem öffentlichen Betrieb `SECRET_KEY` und `APP_URL` in `.env` setzen und
die Einrichtung über `http://<server>:8000/setup` abschließen.

---

## Lizenz

Siehe [LICENSE](LICENSE).
