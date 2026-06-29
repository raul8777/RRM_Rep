-- Migration 017: screener_decisions table for regime-aware pair screening audit trail

CREATE TABLE IF NOT EXISTS screener_decisions (
    id SERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    regime_mode TEXT NOT NULL,
    spot_symbols JSONB,
    futures_symbols JSONB,
    all_symbols JSONB,
    scores_json JSONB,
    parameters_json JSONB,
    rejected_reason TEXT,
    source TEXT DEFAULT 'pair_screener'
);

CREATE INDEX IF NOT EXISTS idx_screener_decisions_created_at ON screener_decisions(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_screener_decisions_regime_mode ON screener_decisions(regime_mode);
