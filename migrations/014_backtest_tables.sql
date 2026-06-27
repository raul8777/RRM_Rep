-- Migration 014: Backtest optimizer tables
-- Trazabilidad 100% de corridas de simulación y optimización de parámetros

CREATE TABLE IF NOT EXISTS backtest_run (
    id              SERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    label           VARCHAR(64),                    -- 'baseline' | 'run_001' | 'optimized'
    window_start    DATE NOT NULL,
    window_end      DATE NOT NULL,
    params          JSONB NOT NULL,                 -- config completo de la corrida
    n_trades        INTEGER DEFAULT 0,
    n_spot          INTEGER DEFAULT 0,
    n_futures       INTEGER DEFAULT 0,
    n_wins          INTEGER DEFAULT 0,
    n_losses        INTEGER DEFAULT 0,
    pnl_total       NUMERIC(12,4) DEFAULT 0,
    pnl_spot        NUMERIC(12,4) DEFAULT 0,
    pnl_futures     NUMERIC(12,4) DEFAULT 0,
    win_rate        NUMERIC(6,4) DEFAULT 0,
    profit_factor   NUMERIC(8,4) DEFAULT 0,
    sharpe          NUMERIC(8,4) DEFAULT 0,
    max_drawdown    NUMERIC(8,4) DEFAULT 0,
    capital_start   NUMERIC(12,4) DEFAULT 0,
    capital_end     NUMERIC(12,4) DEFAULT 0,
    return_pct      NUMERIC(8,4) DEFAULT 0,
    is_baseline     BOOLEAN DEFAULT FALSE,
    rank_pnl        INTEGER,                        -- filled after full run
    duration_ms     INTEGER
);

CREATE TABLE IF NOT EXISTS backtest_trade (
    id              SERIAL PRIMARY KEY,
    run_id          INTEGER NOT NULL REFERENCES backtest_run(id) ON DELETE CASCADE,
    symbol          VARCHAR(20) NOT NULL,
    trade_type      VARCHAR(10) NOT NULL,           -- 'spot' | 'futures'
    side            VARCHAR(5)  NOT NULL,           -- 'BUY' | 'SHORT'
    entry_ts        TIMESTAMPTZ,
    exit_ts         TIMESTAMPTZ,
    entry_price     NUMERIC(18,8),
    exit_price      NUMERIC(18,8),
    qty             NUMERIC(18,6),
    usdt_size       NUMERIC(12,4),
    pnl_usdt        NUMERIC(12,4),
    pnl_pct         NUMERIC(8,4),
    tier            VARCHAR(10),                    -- FUERTE/ALTA/MEDIA/BASE
    regime          VARCHAR(30),
    close_reason    VARCHAR(30),                    -- stop_loss/take_profit/time_exit
    kelly_mult      NUMERIC(6,4),
    indicators      JSONB
);

CREATE INDEX IF NOT EXISTS idx_backtest_run_ts      ON backtest_run(ts DESC);
CREATE INDEX IF NOT EXISTS idx_backtest_run_pnl     ON backtest_run(pnl_total DESC);
CREATE INDEX IF NOT EXISTS idx_backtest_run_base     ON backtest_run(is_baseline);
CREATE INDEX IF NOT EXISTS idx_backtest_trade_run    ON backtest_trade(run_id);
CREATE INDEX IF NOT EXISTS idx_backtest_trade_sym    ON backtest_trade(symbol);
CREATE INDEX IF NOT EXISTS idx_backtest_trade_type   ON backtest_trade(trade_type);
