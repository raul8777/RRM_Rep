-- GIN indexes for JSONB columns used in search/filter
-- Migration: 005_add_jsonb_indexes.sql

-- trades_open metadata search
CREATE INDEX IF NOT EXISTS idx_trades_open_metadata_gin
    ON trades_open USING GIN (metadata jsonb_path_ops)
    WHERE metadata IS NOT NULL;

-- trades_closed metadata search
CREATE INDEX IF NOT EXISTS idx_trades_closed_metadata_gin
    ON trades_closed USING GIN (metadata jsonb_path_ops)
    WHERE metadata IS NOT NULL;

-- trades_closed indicators_at_entry for prediction analysis
CREATE INDEX IF NOT EXISTS idx_trades_closed_indicators_gin
    ON trades_closed USING GIN (indicators_at_entry jsonb_path_ops)
    WHERE indicators_at_entry IS NOT NULL;

-- orders metadata
CREATE INDEX IF NOT EXISTS idx_orders_metadata_gin
    ON orders USING GIN (metadata jsonb_path_ops)
    WHERE metadata IS NOT NULL;

-- market_data JSONB data column
CREATE INDEX IF NOT EXISTS idx_market_data_data_gin
    ON market_data USING GIN (data jsonb_path_ops);

-- ANALYZE all affected tables after creating indexes
ANALYZE trades_open;
ANALYZE trades_closed;
ANALYZE orders;
ANALYZE market_data;
ANALYZE audit_log;
ANALYZE system_state;
