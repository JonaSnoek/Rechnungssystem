-- 003: Guthabenkonto je Person mit Kontobewegungen und Einzahlungen.
--
-- Kontofuehrung nach den Regeln:
--   * Eine Buchung (Verzehr) belastet das Konto SOFORT bei der Erfassung.
--   * Eine Einzahlung ginguenst das Konto.
--   * Der Rechnungsversand belastet das Konto NICHT erneut. Die Rechnung
--     weist nur aus, welcher Teil des Verzehrs durch vorhandenes Guthaben
--     gedeckt war (credit_applied_cents) und wie viel zu zahlen bleibt.
--   * Alle Betraege sind Integer-Cent, niemals Fliesskommazahlen.
--   * Kontobewegungen werden nie geloescht oder ueberschrieben; eine
--     fehlerhafte Buchung wird durch eine neue Korrekturbuchung ausgeglichen.
--
-- Wiederholbarkeit: jede Migration laeuft genau einmal, weil app/migrations.py
-- sie in schema_migrations protokolliert und nur offene Versionen ausfuehrt.
-- Ein zweiter Lauf ist damit ein No-op. Darueber hinaus sichern
-- consumption_id und deposit_id in ledger_entries die Eindeutigkeit: eine
-- zweite Bewegung auf denselben Vorgang ist datenbankseitig ausgeschlossen,
-- unabhaengig davon, wie oft die Rueckuebernahme laeuft.

-- ---------------------------------------------------------------------------
-- Konten
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id INTEGER NOT NULL REFERENCES persons (id) ON DELETE CASCADE,
    currency VARCHAR(3) NOT NULL DEFAULT 'EUR',
    -- negativ = Schuldner, positiv = Guthaben
    balance_cents INTEGER NOT NULL DEFAULT 0,
    total_consumption_cents INTEGER NOT NULL DEFAULT 0,
    total_deposit_cents INTEGER NOT NULL DEFAULT 0,
    total_invoiced_cents INTEGER NOT NULL DEFAULT 0,
    last_entry_at TIMESTAMP,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL,
    CONSTRAINT uq_accounts_person UNIQUE (person_id)
);

CREATE INDEX IF NOT EXISTS ix_accounts_balance ON accounts (balance_cents);

-- ---------------------------------------------------------------------------
-- Einzahlungen
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS deposits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id INTEGER NOT NULL REFERENCES persons (id) ON DELETE CASCADE,
    amount_cents INTEGER NOT NULL,
    currency VARCHAR(3) NOT NULL DEFAULT 'EUR',
    payment_type VARCHAR(20) NOT NULL DEFAULT 'BAR',
    paid_at TIMESTAMP NOT NULL,
    note TEXT,
    created_by VARCHAR(64),
    -- Kontostaende zum Zeitpunkt der Einzahlung; noetig fuer die E-Mail und
    -- damit die Historie unabhaengig vom heutigen Stand lesbar bleibt.
    balance_before_cents INTEGER NOT NULL DEFAULT 0,
    balance_after_cents INTEGER NOT NULL DEFAULT 0,
    -- E-Mail getrennt von der Buchung: sie darf nie die Einzahlung kippen.
    email_status VARCHAR(20) NOT NULL DEFAULT 'OFFEN',
    email_sent_at TIMESTAMP,
    email_error TEXT,
    email_attempts INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL,
    CONSTRAINT ck_deposit_amount_positive CHECK (amount_cents > 0)
);

CREATE INDEX IF NOT EXISTS ix_deposits_person_paid
    ON deposits (person_id, paid_at DESC);

CREATE INDEX IF NOT EXISTS ix_deposits_email_status
    ON deposits (email_status, id);

-- ---------------------------------------------------------------------------
-- Kontobewegungen (nur anhaengen, nie aendern)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ledger_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts (id) ON DELETE CASCADE,
    person_id INTEGER NOT NULL REFERENCES persons (id) ON DELETE CASCADE,
    -- VERZEHR | EINZAHLUNG | EROEFFNUNG | KORREKTUR
    entry_type VARCHAR(20) NOT NULL,
    -- negativ belastet das Konto, positiv ginguenst es
    amount_cents INTEGER NOT NULL,
    balance_before_cents INTEGER NOT NULL,
    balance_after_cents INTEGER NOT NULL,
    consumption_id INTEGER REFERENCES consumptions (id) ON DELETE CASCADE,
    deposit_id INTEGER REFERENCES deposits (id) ON DELETE CASCADE,
    invoice_id INTEGER REFERENCES invoices (id) ON DELETE SET NULL,
    -- Zeiger auf die Bewegung, die diese Korrekturbuchung ausgleicht. Eine
    -- KORREKTUR traegt hier die VERZEHR-Bewegung, ein Wiederaktivieren die
    -- Stornierung. So bleibt die Kette vollstaendig nachvollziehbar und eine
    -- doppelte Umkehrung ist ausgeschlossen.
    reverses_entry_id INTEGER REFERENCES ledger_entries (id) ON DELETE SET NULL,
    -- bei einer Rechnung: welcher Betrag durch Guthaben gedeckt wurde
    credit_applied_cents INTEGER NOT NULL DEFAULT 0,
    note TEXT,
    created_at TIMESTAMP NOT NULL,
    -- Idempotenz: hoechstens eine Bewegung je Buchung bzw. Einzahlung, und
    -- hoechstens eine Umkehrung je Bewegung. NULL bleibt mehrfach erlaubt,
    -- weil das nur fuer die nicht umkehrenden Zeilen zaehlt.
    CONSTRAINT uq_ledger_consumption UNIQUE (consumption_id),
    CONSTRAINT uq_ledger_deposit UNIQUE (deposit_id),
    CONSTRAINT uq_ledger_reverses UNIQUE (reverses_entry_id)
);

CREATE INDEX IF NOT EXISTS ix_ledger_person_created
    ON ledger_entries (person_id, created_at);

CREATE INDEX IF NOT EXISTS ix_ledger_account_created
    ON ledger_entries (account_id, created_at);

-- ---------------------------------------------------------------------------
-- Spalten an bestehenden Tabellen
--
-- Die neuen Spalten sind bewusst nullable bzw. haben 0 als Vorgabe. Die
-- Rueckuebernahme der Bestandsdaten liegt in app/services/accounts.py
-- (ensure_accounts_backfilled) und laeuft nach dem Schema, damit sie Spalten
-- kennt und mehrfach aufrufbar ist, ohne Betraege zu erfinden.
-- ---------------------------------------------------------------------------

-- Guthaben, das diese Buchung bei der Erfassung verbraucht hat.
-- Die Summe ueber die Buchungen einer Rechnung ist das auf der Rechnung
-- verrechnete Guthaben. So wird der Kontostand nicht ein zweites Mal belastet.
ALTER TABLE consumptions ADD COLUMN balance_before_cents INTEGER;
ALTER TABLE consumptions ADD COLUMN credit_applied_cents INTEGER NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS ix_consumptions_person_invoice
    ON consumptions (person_id, invoice_id);

-- Rechnungen: Verrechnungsdaten als Momentaufnahme. credit_applied_cents und
-- amount_due_cents werden beim Erstellen der Rechnung aus den Buchungen
-- uebernommen. Die Rechnung aendert den Kontostand nicht.
ALTER TABLE invoices ADD COLUMN credit_applied_cents INTEGER NOT NULL DEFAULT 0;
ALTER TABLE invoices ADD COLUMN amount_due_cents INTEGER NOT NULL DEFAULT 0;
ALTER TABLE invoices ADD COLUMN balance_before_cents INTEGER;
ALTER TABLE invoices ADD COLUMN balance_after_cents INTEGER;