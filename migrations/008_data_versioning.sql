-- Migration: Data Versioning for Learning Engine
-- Created: Jun 17, 2026
-- Purpose: Mark corrupt/old data to exclude from learning

-- 1. Create system_versions table
CREATE TABLE IF NOT EXISTS system_versions (
    id SERIAL PRIMARY KEY,
    version_tag TEXT NOT NULL UNIQUE,
    description TEXT,
    breaking_changes TEXT[],
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    is_active BOOLEAN DEFAULT FALSE
);

-- 2. Insert version history
INSERT INTO system_versions (version_tag, description, breaking_changes, is_active) VALUES
('v1.0_bootstrap', 'Synthetic bootstrap data (pre-real trading)', ARRAY['synthetic', 'pre_real'], FALSE),
('v1.1_rebalance', 'Portfolio rebalancing era (corrupted logic)', ARRAY['corrupted_rebalance'], FALSE),
('v1.2_volspike_v1', 'Original VolSpike Override (permissive)', ARRAY['volspike_v1'], FALSE),
('v2.0_current', 'VolSpike V2 + Fixed rebalance + Dashboard Audit', ARRAY[]::TEXT[], TRUE)
ON CONFLICT (version_tag) DO NOTHING;

-- 3. Add columns to trades_closed
ALTER TABLE trades_closed 
ADD COLUMN IF NOT EXISTS data_version TEXT DEFAULT 'v1.2_volspike_v1',
ADD COLUMN IF NOT EXISTS is_synthetic BOOLEAN DEFAULT FALSE,
ADD COLUMN IF NOT EXISTS is_corrupted BOOLEAN DEFAULT FALSE,
ADD COLUMN IF NOT EXISTS learning_eligible BOOLEAN DEFAULT TRUE;

-- 4. Create indexes for performance
CREATE INDEX IF NOT EXISTS idx_trades_closed_version ON trades_closed(data_version);
CREATE INDEX IF NOT EXISTS idx_trades_closed_eligible ON trades_closed(learning_eligible, exit_time DESC);
CREATE INDEX IF NOT EXISTS idx_trades_closed_synthetic ON trades_closed(is_synthetic);

-- 5. Create data quality stats view
CREATE OR REPLACE VIEW v_data_quality_stats AS
SELECT 
    COUNT(*) as total_trades,
    COUNT(*) FILTER (WHERE learning_eligible = TRUE) as eligible_trades,
    COUNT(*) FILTER (WHERE is_synthetic = TRUE) as synthetic_trades,
    COUNT(*) FILTER (WHERE is_corrupted = TRUE) as corrupted_trades,
    COUNT(*) FILTER (WHERE data_version = 'v2.0_current') as v2_trades,
    MIN(exit_time) as oldest_trade,
    MAX(exit_time) as newest_trade,
    data_version
FROM trades_closed
GROUP BY data_version;

-- 6. Mark existing corrupted data (portfolio rebalancing trades)
UPDATE trades_closed 
SET is_corrupted = TRUE, 
    learning_eligible = FALSE,
    data_version = 'v1.1_rebalance'
WHERE close_reason IN ('portfolio_rebalancing', 'manual_rebalance', 'prediction_reallocation', 'prediction_realloc')
  AND (is_corrupted IS NULL OR is_corrupted = FALSE);

-- 7. Mark synthetic/bootstrap data (based on metadata or null close_reason patterns)
UPDATE trades_closed 
SET is_synthetic = TRUE,
    learning_eligible = FALSE,
    data_version = 'v1.0_bootstrap'
WHERE (metadata->>'source' = 'bootstrap' OR metadata->>'source' = 'synthetic' OR close_reason = 'bootstrap')
  AND (is_synthetic IS NULL OR is_synthetic = FALSE);

-- 8. Mark recent legitimate trades as v2.0
UPDATE trades_closed 
SET data_version = 'v2.0_current',
    learning_eligible = TRUE,
    is_corrupted = FALSE,
    is_synthetic = FALSE
WHERE exit_time >= '2026-06-16'::timestamp 
  AND (is_corrupted = FALSE OR is_corrupted IS NULL)
  AND (is_synthetic = FALSE OR is_synthetic IS NULL);

-- 9. Show summary of changes
SELECT 
    data_version,
    COUNT(*) as count,
    COUNT(*) FILTER (WHERE learning_eligible = TRUE) as eligible,
    COUNT(*) FILTER (WHERE is_corrupted = TRUE) as corrupted,
    COUNT(*) FILTER (WHERE is_synthetic = TRUE) as synthetic
FROM trades_closed
GROUP BY data_version
ORDER BY data_version;
