-- Migration 015: widen numeric precision for Backtrace 1000 persistence
-- Reason: 90d runs with larger capital can exceed previous NUMERIC(12,4)/(8,4) limits.

ALTER TABLE backtest_run
    ALTER COLUMN pnl_total TYPE NUMERIC(20,4),
    ALTER COLUMN pnl_spot TYPE NUMERIC(20,4),
    ALTER COLUMN pnl_futures TYPE NUMERIC(20,4),
    ALTER COLUMN win_rate TYPE NUMERIC(10,6),
    ALTER COLUMN profit_factor TYPE NUMERIC(18,6),
    ALTER COLUMN sharpe TYPE NUMERIC(18,6),
    ALTER COLUMN max_drawdown TYPE NUMERIC(18,4),
    ALTER COLUMN capital_start TYPE NUMERIC(20,4),
    ALTER COLUMN capital_end TYPE NUMERIC(20,4),
    ALTER COLUMN return_pct TYPE NUMERIC(18,4);

ALTER TABLE backtest_trade
    ALTER COLUMN usdt_size TYPE NUMERIC(20,6),
    ALTER COLUMN pnl_usdt TYPE NUMERIC(20,4),
    ALTER COLUMN pnl_pct TYPE NUMERIC(18,4),
    ALTER COLUMN kelly_mult TYPE NUMERIC(10,6);
