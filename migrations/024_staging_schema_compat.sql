-- Migration 024: schema compatibility for runtime audit/events
-- Aligns legacy check constraints/columns so runtime writes do not fail.

ALTER TABLE IF EXISTS trades_open
    ADD COLUMN IF NOT EXISTS corrupt BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE IF EXISTS trades_closed
    ADD COLUMN IF NOT EXISTS corrupt BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE IF EXISTS trades_audit
    DROP CONSTRAINT IF EXISTS trades_audit_trade_type_check;

ALTER TABLE IF EXISTS trades_audit
    ADD CONSTRAINT trades_audit_trade_type_check
    CHECK (trade_type IN ('spot', 'futures', 'system'));
