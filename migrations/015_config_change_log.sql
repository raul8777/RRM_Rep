-- Migration 015: config change log and snapshot metadata
CREATE TABLE IF NOT EXISTS config_change_log (
    id SERIAL PRIMARY KEY,
    changed_at TIMESTAMPTZ DEFAULT NOW(),
    source VARCHAR(64) NOT NULL,        -- api, scheduler, backtest_apply, rollback, etc.
    changed_by VARCHAR(128),            -- api key / user / process
    snapshot_id VARCHAR(128),           -- optional snapshot reference
    diff JSONB NOT NULL,                -- {param: {old, new}}
    full_config JSONB                   -- optional full dump_active_config snapshot
);

CREATE INDEX IF NOT EXISTS idx_config_change_log_changed_at
    ON config_change_log(changed_at DESC);
CREATE INDEX IF NOT EXISTS idx_config_change_log_source
    ON config_change_log(source);

-- Ensure latest config snapshot metadata is indexable
CREATE INDEX IF NOT EXISTS idx_trading_runtime_state_key
    ON trading_runtime_state(key);
