-- Migration 019: Super MIMIC experiment registry and deployment history
--
-- Adds tables to track Super MIMIC experiments, their artifacts, and every
-- safe deployment of a recommended config. Supports audit trail and rollback.

CREATE TABLE IF NOT EXISTS super_mimic_experiments (
    id VARCHAR(64) PRIMARY KEY,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL,
    status VARCHAR(32) NOT NULL,
    engine VARCHAR(32) NOT NULL,
    mode VARCHAR(32) NOT NULL,
    profile VARCHAR(8) NOT NULL,
    windows JSONB NOT NULL DEFAULT '[]'::jsonb,
    config JSONB NOT NULL DEFAULT '{}'::jsonb,
    artifact_path TEXT NOT NULL,
    metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
    fingerprint JSONB NOT NULL DEFAULT '{}'::jsonb,
    landscape JSONB NOT NULL DEFAULT '{}'::jsonb,
    recommended_config JSONB NOT NULL DEFAULT '{}'::jsonb,
    error TEXT
);

CREATE INDEX IF NOT EXISTS idx_super_mimic_experiments_created_at
    ON super_mimic_experiments (created_at DESC);

CREATE INDEX IF NOT EXISTS idx_super_mimic_experiments_status
    ON super_mimic_experiments (status);

CREATE INDEX IF NOT EXISTS idx_super_mimic_experiments_mode_profile
    ON super_mimic_experiments (mode, profile);

CREATE INDEX IF NOT EXISTS idx_super_mimic_experiments_fingerprint_gin
    ON super_mimic_experiments USING GIN (fingerprint jsonb_path_ops);


CREATE TABLE IF NOT EXISTS super_mimic_deployments (
    id VARCHAR(64) PRIMARY KEY,
    experiment_id VARCHAR(64) NOT NULL,
    deployed_at TIMESTAMP NOT NULL,
    target VARCHAR(64) NOT NULL,  -- e.g. regime_profile / kelly_config_override
    mode VARCHAR(32) NOT NULL,
    profile VARCHAR(8) NOT NULL,
    changes JSONB NOT NULL DEFAULT '{}'::jsonb,
    rollback_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    operator TEXT
);

CREATE INDEX IF NOT EXISTS idx_super_mimic_deployments_experiment_id
    ON super_mimic_deployments (experiment_id);

CREATE INDEX IF NOT EXISTS idx_super_mimic_deployments_deployed_at
    ON super_mimic_deployments (deployed_at DESC);
