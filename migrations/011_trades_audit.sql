-- Migration 011: trades_audit table for full transaction traceability
-- Logs every critical trading event with Binance confirmation status

CREATE TABLE IF NOT EXISTS trades_audit (
    id SERIAL PRIMARY KEY,
    ts TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    symbol VARCHAR(20) NOT NULL,
    trade_type VARCHAR(10) NOT NULL CHECK (trade_type IN ('spot', 'futures')),
    event VARCHAR(40) NOT NULL,
    -- Common events: open, close, sl_update, tp_update, bounce_tag,
    --                sso_trigger, ghost_close, retro_tag, binance_sync
    detail JSONB,
    -- Contains: order_id, price, qty, pnl, reason, side, entry_price,
    --           sl_price, tp_price, peak_pnl_pct, age_hours, etc.
    binance_confirmed BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_trades_audit_ts     ON trades_audit(ts DESC);
CREATE INDEX IF NOT EXISTS idx_trades_audit_symbol ON trades_audit(symbol);
CREATE INDEX IF NOT EXISTS idx_trades_audit_event  ON trades_audit(event);
