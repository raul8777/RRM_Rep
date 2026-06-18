-- Migration: Persistent trading cooldowns
-- Created: Jun 17, 2026
-- Purpose: Persist cooldown state across Docker restarts to prevent
--          double-entry in NARRATIVE/VolSpike tokens after restart.

CREATE TABLE IF NOT EXISTS trading_cooldowns (
    symbol          TEXT NOT NULL,
    cooldown_type   TEXT NOT NULL,   -- 'volspike' | 'narrative' | 'override_daily'
    activated_at    TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    expires_at      TIMESTAMP WITH TIME ZONE NOT NULL,
    metadata        JSONB,
    PRIMARY KEY (symbol, cooldown_type)
);

CREATE INDEX IF NOT EXISTS idx_trading_cooldowns_expires
    ON trading_cooldowns(expires_at);

-- Auto-purge expired rows on insert (keep table lean for 24/7 operation)
CREATE OR REPLACE FUNCTION purge_expired_cooldowns()
RETURNS TRIGGER AS $$
BEGIN
    DELETE FROM trading_cooldowns WHERE expires_at < NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_purge_cooldowns ON trading_cooldowns;
CREATE TRIGGER trg_purge_cooldowns
    AFTER INSERT ON trading_cooldowns
    EXECUTE FUNCTION purge_expired_cooldowns();
