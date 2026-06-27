-- Migration 012: trading_runtime_state table
-- Purpose: Persistent runtime state for trading engine.
--          Provides restart-safe storage for values that were previously
--          held only in RAM (daily PnL, regime confidence, learning status).
-- Keys are upserted by the application — one row per key.
-- 
-- Initial keys populated by the application:
--   daily_pnl_futures   — today's realized PnL from futures closes
--   daily_pnl_spot      — today's realized PnL from spot closes
--   regime_confidence   — current regime mode + confidence score
--   last_learning_run   — timestamp + stats from last LearningEngine run
--   analysis_cache_freshness — last analysis cycle timestamp + symbols count

CREATE TABLE IF NOT EXISTS trading_runtime_state (
    key        TEXT PRIMARY KEY,
    value      JSONB NOT NULL DEFAULT '{}',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_trading_runtime_state_updated
    ON trading_runtime_state (updated_at DESC);

-- Seed with empty defaults so the endpoint always returns all keys
INSERT INTO trading_runtime_state (key, value) VALUES
    ('daily_pnl_futures',        '{"pnl": 0.0, "trades": 0}'),
    ('daily_pnl_spot',           '{"pnl": 0.0, "trades": 0}'),
    ('regime_confidence',        '{"mode": "UNKNOWN", "confidence": 0.0, "confirmed": false}'),
    ('last_learning_run',        '{"ts": null, "trigger": null, "symbols": 0}'),
    ('analysis_cache_freshness', '{"ts": null, "symbols": [], "cycle": 0}')
ON CONFLICT (key) DO NOTHING;
