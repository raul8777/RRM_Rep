-- Add PnL prediction fields to trades tables
-- Migration: 003_add_pnl_prediction.sql

-- Add fields to trades_open table
ALTER TABLE trades_open 
ADD COLUMN IF NOT EXISTS pnl_prediction JSONB,
ADD COLUMN IF NOT EXISTS indicators_at_entry JSONB;

-- Add fields to trades_closed table
ALTER TABLE trades_closed 
ADD COLUMN IF NOT EXISTS pnl_prediction JSONB,
ADD COLUMN IF NOT EXISTS indicators_at_entry JSONB,
ADD COLUMN IF NOT EXISTS prediction_accuracy NUMERIC(10, 4);

-- Create indexes for prediction analysis
CREATE INDEX IF NOT EXISTS idx_trades_closed_prediction_accuracy ON trades_closed(prediction_accuracy);
CREATE INDEX IF NOT EXISTS idx_trades_open_pnl_prediction ON trades_open USING GIN (pnl_prediction);
CREATE INDEX IF NOT EXISTS idx_trades_closed_pnl_prediction ON trades_closed USING GIN (pnl_prediction);
