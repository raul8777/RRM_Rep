-- Migration 018: Add regime column to indicator_weights for regime-aware learned weights (BUG-6)
--
-- The indicator_weights table previously used (symbol) as the unique key, meaning
-- weights learned in one regime would be served in all subsequent regimes until
-- they aged out (7-day TTL). This migration adds a regime column so weights are
-- stored and looked up per (symbol, regime), enabling same-cycle freshness when
-- the system switches market regimes.
--
-- Backward compatible: existing rows get regime = '__any__' which is the default
-- fallback used by _load_weights_from_db when no regime-specific row exists.

ALTER TABLE indicator_weights
    ADD COLUMN IF NOT EXISTS regime VARCHAR(32) NOT NULL DEFAULT '__any__';

-- Drop the old single-column unique constraint and replace with (symbol, regime).
-- The original constraint name varies by deployment so we attempt both common names
-- and fall back silently if they don't exist.
DO $$
BEGIN
    -- Attempt to drop the legacy unique index on symbol alone
    BEGIN
        ALTER TABLE indicator_weights DROP CONSTRAINT IF EXISTS indicator_weights_symbol_key;
    EXCEPTION WHEN OTHERS THEN NULL;
    END;
    BEGIN
        ALTER TABLE indicator_weights DROP CONSTRAINT IF EXISTS uq_indicator_weights_symbol;
    EXCEPTION WHEN OTHERS THEN NULL;
    END;
END;
$$;

CREATE UNIQUE INDEX IF NOT EXISTS idx_iw_symbol_regime
    ON indicator_weights (symbol, regime);
