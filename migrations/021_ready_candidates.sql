-- Migration 021: ready_candidates and ready_candidate_checks
-- Persistent staged-entry state for multi-cycle validation.

CREATE TABLE IF NOT EXISTS ready_candidates (
    id BIGSERIAL PRIMARY KEY,
    symbol TEXT NOT NULL,
    trade_type TEXT NOT NULL DEFAULT 'spot',
    regime_mode TEXT NOT NULL,
    profile_label TEXT,
    signal_price NUMERIC,
    position_size_usdt NUMERIC,

    status TEXT NOT NULL CHECK (status IN ('ready','validated','opened','discarded','expired')),
    positive_checks INTEGER NOT NULL DEFAULT 0,
    total_checks INTEGER NOT NULL DEFAULT 0,
    required_positive_checks INTEGER NOT NULL DEFAULT 2,
    checks_per_cycle INTEGER NOT NULL DEFAULT 1,
    max_cycles INTEGER NOT NULL DEFAULT 2,
    max_age_min INTEGER NOT NULL DEFAULT 25,

    ready_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_checked_at TIMESTAMPTZ,
    validated_at TIMESTAMPTZ,
    opened_at TIMESTAMPTZ,
    discarded_at TIMESTAMPTZ,
    discard_reason TEXT,

    opened_trade_id BIGINT,
    signal_meta JSONB NOT NULL DEFAULT '{}',
    config_snapshot JSONB NOT NULL DEFAULT '{}',
    data JSONB NOT NULL DEFAULT '{}',

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Only one active ready/validated row per symbol/side/regime.
CREATE UNIQUE INDEX IF NOT EXISTS idx_ready_candidates_active
    ON ready_candidates(symbol, trade_type, regime_mode)
    WHERE status IN ('ready','validated');

CREATE INDEX IF NOT EXISTS idx_ready_candidates_status_regime
    ON ready_candidates(status, regime_mode, ready_at DESC);

CREATE INDEX IF NOT EXISTS idx_ready_candidates_opened_trade
    ON ready_candidates(opened_trade_id) WHERE opened_trade_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS ready_candidate_checks (
    id BIGSERIAL PRIMARY KEY,
    ready_candidate_id BIGINT NOT NULL REFERENCES ready_candidates(id) ON DELETE CASCADE,
    cycle_id TEXT,
    check_sequence INTEGER NOT NULL,
    check_type TEXT NOT NULL CHECK (check_type IN ('intra','inter','manual')),
    checked_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    current_price NUMERIC,
    price_source TEXT,
    expected_pnl_pct NUMERIC,
    quality_score NUMERIC,
    profile_label TEXT,
    thresholds_snapshot JSONB,
    fear_greed NUMERIC,

    input_features JSONB NOT NULL DEFAULT '{}',
    positive BOOLEAN NOT NULL,
    decision TEXT NOT NULL,  -- positive, negative, validated, discarded, expired
    reason TEXT,
    execution_ms INTEGER,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ready_checks_candidate_seq
    ON ready_candidate_checks(ready_candidate_id, check_sequence);

CREATE INDEX IF NOT EXISTS idx_ready_checks_cycle
    ON ready_candidate_checks(cycle_id, check_type);
