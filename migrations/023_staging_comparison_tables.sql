-- Migration 023: staging comparison tables
-- Purpose: persist decisions that compare prod-vs-staging under identical market input.
--          This is used for evidence dashboards and rollback analysis, not for real execution.

CREATE TABLE IF NOT EXISTS staging_decision_compare (
    id SERIAL PRIMARY KEY,
    ts TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    decision_run_id TEXT NOT NULL,
    parent_trade_ref TEXT,
    market_snapshot_id TEXT,
    decision_domain TEXT NOT NULL DEFAULT 'general',
    status TEXT NOT NULL DEFAULT 'stable',
    prod_selected BOOLEAN NOT NULL DEFAULT FALSE,
    staging_selected BOOLEAN NOT NULL DEFAULT FALSE,
    delta_total NUMERIC(18,8) NOT NULL DEFAULT 0,
    reason_prod TEXT,
    reason_staging TEXT,
    config_hash_prod TEXT,
    config_hash_staging TEXT,
    migration_hash_prod TEXT,
    migration_hash_staging TEXT,
    prod_version TEXT,
    staging_version TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_staging_compare_ts
    ON staging_decision_compare (ts DESC);

CREATE INDEX IF NOT EXISTS idx_staging_compare_domain
    ON staging_decision_compare (decision_domain);

CREATE INDEX IF NOT EXISTS idx_staging_compare_run_id
    ON staging_decision_compare (decision_run_id);

CREATE INDEX IF NOT EXISTS idx_staging_compare_parent_trade_ref
    ON staging_decision_compare (parent_trade_ref);

COMMENT ON TABLE staging_decision_compare IS
    'Comparison ledger for production-vs-staging decisions. This is evidence-only and never drives real execution.';
