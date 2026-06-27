-- Migration 013: kelly_sizing_log — trazabilidad 100% de decisiones Kelly sizing
-- Autor: Cascade (K1/C3 plan kelly-agresivo-config-panel-863057)
-- Jun 26 2026

CREATE TABLE IF NOT EXISTS kelly_sizing_log (
    id          BIGSERIAL PRIMARY KEY,
    ts          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    symbol      TEXT        NOT NULL,
    trade_type  TEXT        NOT NULL DEFAULT 'spot',  -- 'spot' | 'futures'
    regime      TEXT,
    prob        FLOAT,
    strength    FLOAT,
    tier        TEXT,        -- 'FUERTE' | 'ALTA' | 'MEDIA' | 'BASE'
    base_usdt   FLOAT,
    sized_usdt  FLOAT,
    final_usdt  FLOAT,
    multipliers JSONB        DEFAULT '{}',  -- {"fng": 1.5, "regime": 0.8, "corr": 0.95, "m3": 1.5}
    liquid_usdt FLOAT,
    cap_usdt    FLOAT,
    action      TEXT         DEFAULT 'open'  -- 'open' | 'skipped' | 'blocked'
);

CREATE INDEX IF NOT EXISTS idx_kelly_log_ts     ON kelly_sizing_log(ts DESC);
CREATE INDEX IF NOT EXISTS idx_kelly_log_symbol ON kelly_sizing_log(symbol, ts DESC);
CREATE INDEX IF NOT EXISTS idx_kelly_log_tier   ON kelly_sizing_log(tier, ts DESC);
CREATE INDEX IF NOT EXISTS idx_kelly_log_type   ON kelly_sizing_log(trade_type, ts DESC);

-- Purge automático: retener solo 30 días
-- (ejecutado periódicamente por orchestrator)
-- DELETE FROM kelly_sizing_log WHERE ts < NOW() - INTERVAL '30 days';
