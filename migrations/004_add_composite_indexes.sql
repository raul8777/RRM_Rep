-- Composite and performance indexes for frequent queries
-- Migration: 004_add_composite_indexes.sql

-- trades_open: filter by status and symbol together
CREATE INDEX IF NOT EXISTS idx_trades_open_symbol_status
    ON trades_open(symbol, status);

-- trades_open: analysis by symbol ordered by time
CREATE INDEX IF NOT EXISTS idx_trades_open_symbol_entry_time
    ON trades_open(symbol, entry_time DESC);

-- trades_closed: most common query - recent trades for a symbol
CREATE INDEX IF NOT EXISTS idx_trades_closed_symbol_exit_time
    ON trades_closed(symbol, exit_time DESC);

-- trades_closed: pnl aggregate queries (day/week/year buckets)
CREATE INDEX IF NOT EXISTS idx_trades_closed_exit_time_desc
    ON trades_closed(exit_time DESC);

-- trades_closed: win-rate / profit-factor calculations
CREATE INDEX IF NOT EXISTS idx_trades_closed_pnl
    ON trades_closed(pnl);

-- orders: status + symbol lookup
CREATE INDEX IF NOT EXISTS idx_orders_symbol_status_created
    ON orders(symbol, status, created_at DESC);

-- market_data: triple-column lookup used in analysis cycle
CREATE INDEX IF NOT EXISTS idx_market_data_symbol_type_ts
    ON market_data(symbol, data_type, timestamp DESC);

-- market_data: cleanup expired entries
CREATE INDEX IF NOT EXISTS idx_market_data_expires_at
    ON market_data(expires_at)
    WHERE expires_at IS NOT NULL;

-- audit_log: recent events by type
CREATE INDEX IF NOT EXISTS idx_audit_log_ts_event
    ON audit_log(timestamp DESC, event_type);

-- system_state: always read the latest row
CREATE INDEX IF NOT EXISTS idx_system_state_changed_at_desc
    ON system_state(changed_at DESC);
