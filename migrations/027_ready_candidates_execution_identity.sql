ALTER TABLE IF EXISTS ready_candidates
    ADD COLUMN IF NOT EXISTS execution_mode TEXT,
    ADD COLUMN IF NOT EXISTS side TEXT;

UPDATE ready_candidates
SET execution_mode = COALESCE(
        NULLIF(UPPER(signal_meta->'directional_arbitration'->>'selected_mode'), ''),
        NULLIF(UPPER(signal_meta->>'selected_mode'), ''),
        CASE
            WHEN trade_type = 'spot_margin' THEN 'SPOT_MARGIN_SHORT'
            WHEN trade_type = 'futures' AND UPPER(regime_mode) = 'FUTURES_SHORT' THEN 'FUTURES_SHORT'
            WHEN trade_type = 'futures' THEN 'FUTURES_LONG'
            ELSE 'SPOT_LONG'
        END
    ),
    side = COALESCE(
        NULLIF(UPPER(signal_meta->>'expected_side'), ''),
        CASE
            WHEN trade_type = 'spot_margin' OR UPPER(regime_mode) = 'FUTURES_SHORT' THEN 'SHORT'
            ELSE 'LONG'
        END
    )
WHERE execution_mode IS NULL OR side IS NULL;

ALTER TABLE ready_candidates
    ALTER COLUMN execution_mode SET NOT NULL,
    ALTER COLUMN side SET NOT NULL,
    ADD CONSTRAINT ready_candidates_execution_mode_check
        CHECK (execution_mode IN ('SPOT_LONG', 'FUTURES_LONG', 'FUTURES_SHORT', 'SPOT_MARGIN_SHORT', 'ABSTAIN')),
    ADD CONSTRAINT ready_candidates_side_check
        CHECK (side IN ('LONG', 'SHORT'));

DROP INDEX IF EXISTS idx_ready_candidates_active;
CREATE UNIQUE INDEX idx_ready_candidates_active
    ON ready_candidates(symbol, execution_mode, regime_mode)
    WHERE status IN ('ready','validated');

CREATE INDEX IF NOT EXISTS idx_ready_candidates_active_side
    ON ready_candidates(regime_mode, side, status, ready_at DESC);