-- Migration: Add UNIQUE index to market_data(symbol, data_type)
-- Created: Jun 20, 2026
-- Purpose: Enable ON CONFLICT (symbol, data_type) DO UPDATE in analysis_cache.py
--          Without this index, the upsert silently fails and falls back to in-memory
--          cache, losing all persisted technical analysis on Docker restart.

-- Partial UNIQUE index: only enforce uniqueness for data_type='technical'
-- (analysis_cache.py upserts — one row per symbol).
-- signal_store.py intentionally inserts multiple rows per symbol (data_type='signal'),
-- so a global UNIQUE(symbol, data_type) would break it.
CREATE UNIQUE INDEX IF NOT EXISTS idx_market_data_symbol_type_technical
    ON market_data(symbol, data_type)
    WHERE data_type = 'technical';
