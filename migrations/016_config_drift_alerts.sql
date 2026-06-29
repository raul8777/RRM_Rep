-- Migration 016: config drift alerts for SPOT+FUTURES baseline monitoring
CREATE TABLE IF NOT EXISTS config_drift_alerts (
    id SERIAL PRIMARY KEY,
    detected_at TIMESTAMPTZ DEFAULT NOW(),
    severity VARCHAR(16) NOT NULL,  -- warning, critical, info
    param VARCHAR(128) NOT NULL,
    expected JSONB NOT NULL,
    actual JSONB NOT NULL,
    source VARCHAR(64) NOT NULL,    -- canonical_default, backtest_baseline
    acknowledged BOOLEAN DEFAULT FALSE,
    acknowledged_at TIMESTAMPTZ,
    acknowledged_by VARCHAR(128),
    notes TEXT
);

CREATE INDEX IF NOT EXISTS idx_config_drift_alerts_detected_at
    ON config_drift_alerts(detected_at DESC);
CREATE INDEX IF NOT EXISTS idx_config_drift_alerts_acknowledged
    ON config_drift_alerts(acknowledged);
CREATE INDEX IF NOT EXISTS idx_config_drift_alerts_severity
    ON config_drift_alerts(severity);
