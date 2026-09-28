-- 001: Ausgangsschema
-- Administrator, Einstellungen, Personen, Produkte, Verzehr, Abrechnungen,
-- Betriebszustaende (Scheduler, Login-Attempte, Audit-Log).

CREATE TABLE admin_users (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    username            VARCHAR(64)  NOT NULL UNIQUE,
    email               VARCHAR(254) NOT NULL UNIQUE,
    password_hash       VARCHAR(255) NOT NULL,
    display_name        VARCHAR(120),
    totp_secret         VARCHAR(64),
    is_active           BOOLEAN      NOT NULL DEFAULT 1,
    last_login_at       TIMESTAMP,
    failed_login_count  INTEGER      NOT NULL DEFAULT 0,
    locked_until        TIMESTAMP,
    created_at          TIMESTAMP    NOT NULL,
    updated_at          TIMESTAMP    NOT NULL
);

CREATE TABLE settings (
    key         VARCHAR(80) PRIMARY KEY,
    value       TEXT,
    is_secret   BOOLEAN      NOT NULL DEFAULT 0,
    "group"     VARCHAR(40)  NOT NULL DEFAULT 'allgemein',
    created_at  TIMESTAMP    NOT NULL,
    updated_at  TIMESTAMP    NOT NULL
);

CREATE TABLE persons (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    first_name  VARCHAR(80)  NOT NULL,
    last_name   VARCHAR(80)  NOT NULL,
    email       VARCHAR(254) NOT NULL,
    phone       VARCHAR(40),
    notes       TEXT,
    is_active   BOOLEAN      NOT NULL DEFAULT 1,
    created_at  TIMESTAMP    NOT NULL,
    updated_at  TIMESTAMP    NOT NULL
);

CREATE INDEX ix_persons_active ON persons (is_active);
CREATE INDEX ix_persons_name ON persons (last_name, first_name);

CREATE TABLE products (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        VARCHAR(120) NOT NULL UNIQUE,
    description TEXT,
    price_cents INTEGER      NOT NULL DEFAULT 0,
    category    VARCHAR(60),
    is_active   BOOLEAN      NOT NULL DEFAULT 1,
    created_at  TIMESTAMP    NOT NULL,
    updated_at  TIMESTAMP    NOT NULL
);

CREATE INDEX ix_products_active ON products (is_active);

CREATE TABLE invoices (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_number      VARCHAR(40)  NOT NULL UNIQUE,
    person_id           INTEGER      NOT NULL REFERENCES persons (id) ON DELETE CASCADE,
    period_date         DATE         NOT NULL,
    sequence            INTEGER      NOT NULL DEFAULT 1,
    total_cents         INTEGER      NOT NULL DEFAULT 0,
    currency            VARCHAR(3)   NOT NULL DEFAULT 'EUR',
    status              VARCHAR(32)  NOT NULL DEFAULT 'OFFEN',
    payment_status      VARCHAR(32)  NOT NULL DEFAULT 'OFFEN',
    paypal_link         VARCHAR(300),
    paypal_username     VARCHAR(40),
    email_subject       TEXT,
    email_body          TEXT,
    email_html          TEXT,
    send_attempts       INTEGER      NOT NULL DEFAULT 0,
    last_error          TEXT,
    is_automatic        BOOLEAN      NOT NULL DEFAULT 1,
    created_by          VARCHAR(64),
    created_at          TIMESTAMP    NOT NULL,
    sent_at             TIMESTAMP,
    paid_at             TIMESTAMP,
    paid_amount_cents   INTEGER,
    CONSTRAINT uq_invoice_person_day_sequence UNIQUE (person_id, period_date, sequence)
);

CREATE INDEX ix_invoices_period_status ON invoices (period_date, status);
CREATE INDEX ix_invoices_person ON invoices (person_id);
CREATE INDEX ix_invoices_payment_status ON invoices (payment_status);

CREATE TABLE consumptions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id           INTEGER      NOT NULL REFERENCES persons (id) ON DELETE CASCADE,
    product_id          INTEGER      REFERENCES products (id) ON DELETE SET NULL,
    product_name        VARCHAR(120) NOT NULL,
    product_description TEXT,
    unit_price_cents    INTEGER      NOT NULL,
    quantity            INTEGER      NOT NULL DEFAULT 1,
    total_cents         INTEGER      NOT NULL DEFAULT 0,
    currency            VARCHAR(3)   NOT NULL DEFAULT 'EUR',
    status              VARCHAR(32)  NOT NULL DEFAULT 'OFFEN',
    note                VARCHAR(255),
    batch_id            VARCHAR(32),
    created_at          TIMESTAMP    NOT NULL,
    invoiced_at         TIMESTAMP,
    invoice_id          INTEGER      REFERENCES invoices (id) ON DELETE CASCADE
);

CREATE INDEX ix_consumptions_person_status ON consumptions (person_id, status);
CREATE INDEX ix_consumptions_created ON consumptions (created_at);
CREATE INDEX ix_consumptions_status_created ON consumptions (status, created_at);
CREATE INDEX ix_consumptions_batch ON consumptions (batch_id);

CREATE TABLE invoice_items (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id       INTEGER      NOT NULL REFERENCES invoices (id) ON DELETE CASCADE,
    product_name     VARCHAR(120) NOT NULL,
    unit_price_cents INTEGER      NOT NULL,
    quantity         INTEGER      NOT NULL,
    total_cents      INTEGER      NOT NULL,
    position         INTEGER      NOT NULL DEFAULT 0
);

CREATE INDEX ix_invoice_items_invoice ON invoice_items (invoice_id);

CREATE TABLE billing_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    period_date     DATE        NOT NULL,
    started_at      TIMESTAMP   NOT NULL,
    finished_at     TIMESTAMP,
    trigger         VARCHAR(20) NOT NULL DEFAULT 'automatic',
    is_catchup      BOOLEAN     NOT NULL DEFAULT 0,
    persons_count   INTEGER     NOT NULL DEFAULT 0,
    invoices_created INTEGER    NOT NULL DEFAULT 0,
    emails_sent     INTEGER     NOT NULL DEFAULT 0,
    emails_failed   INTEGER     NOT NULL DEFAULT 0,
    total_cents     INTEGER     NOT NULL DEFAULT 0,
    status          VARCHAR(20) NOT NULL DEFAULT 'running',
    message         TEXT
);

CREATE INDEX ix_billing_runs_period ON billing_runs (period_date);

CREATE TABLE scheduler_state (
    id                   INTEGER PRIMARY KEY,
    last_success_at      TIMESTAMP,
    last_error           TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_billed_period   DATE,
    last_tick_at         TIMESTAMP,
    heartbeat            TIMESTAMP,
    updated_at           TIMESTAMP NOT NULL
);

CREATE TABLE login_attempts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    identifier   VARCHAR(160) NOT NULL,
    ip_address   VARCHAR(64)  NOT NULL DEFAULT '',
    success      BOOLEAN      NOT NULL DEFAULT 0,
    attempted_at TIMESTAMP    NOT NULL
);

CREATE INDEX ix_login_attempts_identifier_time ON login_attempts (identifier, attempted_at);

CREATE TABLE audit_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    actor      VARCHAR(120),
    action     VARCHAR(80) NOT NULL,
    target     VARCHAR(160),
    detail     TEXT,
    ip_address VARCHAR(64) NOT NULL DEFAULT '',
    created_at TIMESTAMP  NOT NULL
);

CREATE INDEX ix_audit_created ON audit_log (created_at);

CREATE TABLE email_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id INTEGER REFERENCES invoices (id) ON DELETE SET NULL,
    recipient  VARCHAR(254) NOT NULL,
    subject    TEXT         NOT NULL,
    status     VARCHAR(20)  NOT NULL,
    error      TEXT,
    created_at TIMESTAMP    NOT NULL
);

CREATE INDEX ix_email_log_created ON email_log (created_at);

CREATE TABLE setup_state (
    id           INTEGER PRIMARY KEY,
    completed    BOOLEAN   NOT NULL DEFAULT 0,
    completed_at TIMESTAMP,
    current_step INTEGER   NOT NULL DEFAULT 1,
    wizard_data  TEXT
);

CREATE TABLE app_meta (
    key        VARCHAR(80) PRIMARY KEY,
    value      TEXT,
    updated_at TIMESTAMP NOT NULL
);
