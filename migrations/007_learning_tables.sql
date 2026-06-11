-- Migration 007: Self-learning tables for backtester, optimizer and learning engine
-- backtest_results  — stores every walk-forward backtest run
-- indicator_weights — adaptive per-symbol indicator weights learned from real trades
-- optimized_params  — best parameter set found by the optimizer per run

CREATE TABLE IF NOT EXISTS backtest_results (
    id              SERIAL PRIMARY KEY,
    run_id          UUID NOT NULL DEFAULT gen_random_uuid(),
    symbol          VARCHAR(20) NOT NULL,
    interval        VARCHAR(10) NOT NULL DEFAULT '1h',
    days_back       INTEGER NOT NULL DEFAULT 30,
    strategy_name   VARCHAR(100) NOT NULL,
    total_trades    INTEGER NOT NULL DEFAULT 0,
    winning_trades  INTEGER NOT NULL DEFAULT 0,
    losing_trades   INTEGER NOT NULL DEFAULT 0,
    win_rate        FLOAT NOT NULL DEFAULT 0.0,
    total_pnl       FLOAT NOT NULL DEFAULT 0.0,
    total_pnl_pct   FLOAT NOT NULL DEFAULT 0.0,
    max_drawdown    FLOAT NOT NULL DEFAULT 0.0,
    sharpe_ratio    FLOAT NOT NULL DEFAULT 0.0,
    sortino_ratio   FLOAT NOT NULL DEFAULT 0.0,
    profit_factor   FLOAT NOT NULL DEFAULT 0.0,
    initial_capital FLOAT NOT NULL DEFAULT 10000.0,
    final_capital   FLOAT NOT NULL DEFAULT 10000.0,
    start_date      TIMESTAMPTZ,
    end_date        TIMESTAMPTZ,
    params          JSONB,
    metadata        JSONB,
    status          VARCHAR(20) NOT NULL DEFAULT 'completed',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_backtest_results_symbol     ON backtest_results (symbol, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_backtest_results_run_id     ON backtest_results (run_id);
CREATE INDEX IF NOT EXISTS idx_backtest_results_created_at ON backtest_results (created_at DESC);


CREATE TABLE IF NOT EXISTS indicator_weights (
    id              SERIAL PRIMARY KEY,
    symbol          VARCHAR(20) NOT NULL DEFAULT '__global__',
    rsi_weight      FLOAT NOT NULL DEFAULT 0.15,
    macd_weight     FLOAT NOT NULL DEFAULT 0.10,
    bb_weight       FLOAT NOT NULL DEFAULT 0.10,
    ema_weight      FLOAT NOT NULL DEFAULT 0.10,
    volume_weight   FLOAT NOT NULL DEFAULT 0.05,
    bias_correction FLOAT NOT NULL DEFAULT 1.0,
    trades_analyzed INTEGER NOT NULL DEFAULT 0,
    win_rate        FLOAT NOT NULL DEFAULT 0.0,
    prediction_accuracy FLOAT NOT NULL DEFAULT 0.0,
    avg_prediction_error FLOAT NOT NULL DEFAULT 0.0,
    signal_quality_score FLOAT NOT NULL DEFAULT 0.0,
    source          VARCHAR(50) NOT NULL DEFAULT 'learning_engine',
    metadata        JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_indicator_weights_symbol ON indicator_weights (symbol);
CREATE INDEX IF NOT EXISTS idx_indicator_weights_updated_at    ON indicator_weights (updated_at DESC);


CREATE TABLE IF NOT EXISTS optimized_params (
    id                  SERIAL PRIMARY KEY,
    run_id              UUID NOT NULL DEFAULT gen_random_uuid(),
    symbol              VARCHAR(20) NOT NULL DEFAULT '__global__',
    rsi_period          INTEGER NOT NULL DEFAULT 14,
    macd_fast           INTEGER NOT NULL DEFAULT 12,
    macd_slow           INTEGER NOT NULL DEFAULT 26,
    macd_signal         INTEGER NOT NULL DEFAULT 9,
    bb_period           INTEGER NOT NULL DEFAULT 20,
    bb_std              FLOAT   NOT NULL DEFAULT 2.0,
    buy_score_threshold INTEGER NOT NULL DEFAULT 2,
    ema_fast_period     INTEGER NOT NULL DEFAULT 9,
    ema_slow_period     INTEGER NOT NULL DEFAULT 21,
    sharpe_ratio        FLOAT NOT NULL DEFAULT 0.0,
    win_rate            FLOAT NOT NULL DEFAULT 0.0,
    total_pnl           FLOAT NOT NULL DEFAULT 0.0,
    is_active           BOOLEAN NOT NULL DEFAULT FALSE,
    backtest_run_id     UUID REFERENCES backtest_results(run_id) ON DELETE SET NULL,
    metadata            JSONB,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_optimized_params_symbol    ON optimized_params (symbol, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_optimized_params_is_active ON optimized_params (symbol, is_active) WHERE is_active = TRUE;


-- Trigger: update updated_at on indicator_weights
CREATE OR REPLACE FUNCTION update_indicator_weights_ts()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN NEW.updated_at = NOW(); RETURN NEW; END;
$$;

DROP TRIGGER IF EXISTS trg_indicator_weights_updated_at ON indicator_weights;
CREATE TRIGGER trg_indicator_weights_updated_at
    BEFORE UPDATE ON indicator_weights
    FOR EACH ROW EXECUTE FUNCTION update_indicator_weights_ts();


-- Seed global default weights (upsert)
INSERT INTO indicator_weights (symbol, rsi_weight, macd_weight, bb_weight, ema_weight, volume_weight, bias_correction, source)
VALUES ('__global__', 0.15, 0.10, 0.10, 0.10, 0.05, 1.0, 'default')
ON CONFLICT (symbol) DO NOTHING;
