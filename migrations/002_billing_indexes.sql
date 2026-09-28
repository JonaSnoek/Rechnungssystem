-- 002: Indizes fuer die Tagesabrechnung und Personenuebersicht.
-- Optimiert die Gruppierung offener Buchungen nach Person und Tag.

CREATE INDEX ix_consumptions_status_person_created
    ON consumptions (status, person_id, created_at);

CREATE INDEX ix_invoices_status_payment ON invoices (status, payment_status);

CREATE INDEX ix_invoices_person_period ON invoices (person_id, period_date);

-- Schnelle Suche nach offenen Betrag je Person
CREATE INDEX ix_consumptions_open_person ON consumptions (person_id, status)
    WHERE status = 'OFFEN';
