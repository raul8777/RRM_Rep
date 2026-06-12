-- Initial database schema for crypto trading system
-- Migration: 001_initial_schema.sql

-- Strategies table
CREATE TABLE IF NOT EXISTS strategies (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT,
    parameters JSONB,
    is_active BOOLEAN DEFAULT true,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Market data table
CREATE TABLE IF NOT EXISTS market_data (
    id SERIAL PRIMARY KEY,
    symbol TEXT NOT NULL,
    data_type TEXT NOT NULL, -- 'technical', 'news', 'signal'
    data JSONB NOT NULL,
    timestamp TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    expires_at TIMESTAMP WITH TIME ZONE
);

CREATE INDEX IF NOT EXISTS idx_market_data_symbol ON market_data(symbol);
CREATE INDEX IF NOT EXISTS idx_market_data_type ON market_data(data_type);
CREATE INDEX IF NOT EXISTS idx_market_data_timestamp ON market_data(timestamp);

-- Trades open table
CREATE TABLE IF NOT EXISTS trades_open (
    id SERIAL PRIMARY KEY,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    quantity NUMERIC(20, 8) NOT NULL,
    price NUMERIC(20, 8) NOT NULL,
    strategy_id INTEGER REFERENCES strategies(id),
    entry_time TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    stop_loss NUMERIC(20, 8),
    take_profit NUMERIC(20, 8),
    status TEXT DEFAULT 'OPEN' CHECK (status IN ('OPEN', 'PENDING_CLOSE')),
    metadata JSONB,
    entry_commission NUMERIC(20, 8)
);

CREATE INDEX IF NOT EXISTS idx_trades_open_symbol ON trades_open(symbol);
CREATE INDEX IF NOT EXISTS idx_trades_open_status ON trades_open(status);
CREATE INDEX IF NOT EXISTS idx_trades_open_entry_time ON trades_open(entry_time);

-- Trades closed table
CREATE TABLE IF NOT EXISTS trades_closed (
    id SERIAL PRIMARY KEY,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    quantity NUMERIC(20, 8) NOT NULL,
    entry_price NUMERIC(20, 8) NOT NULL,
    exit_price NUMERIC(20, 8) NOT NULL,
    strategy_id INTEGER REFERENCES strategies(id),
    entry_time TIMESTAMP WITH TIME ZONE NOT NULL,
    exit_time TIMESTAMP WITH TIME ZONE NOT NULL,
    pnl NUMERIC(20, 8),
    pnl_percentage NUMERIC(10, 4),
    close_reason TEXT,
    metadata JSONB,
    entry_commission NUMERIC(20, 8),
    exit_commission NUMERIC(20, 8)
);

CREATE INDEX IF NOT EXISTS idx_trades_closed_symbol ON trades_closed(symbol);
CREATE INDEX IF NOT EXISTS idx_trades_closed_entry_time ON trades_closed(entry_time);
CREATE INDEX IF NOT EXISTS idx_trades_closed_exit_time ON trades_closed(exit_time);

-- Orders table
CREATE TABLE IF NOT EXISTS orders (
    id SERIAL PRIMARY KEY,
    trade_open_id INTEGER REFERENCES trades_open(id),
    trade_closed_id INTEGER REFERENCES trades_closed(id),
    symbol TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    order_type TEXT NOT NULL CHECK (order_type IN ('MARKET', 'LIMIT', 'STOP_LIMIT')),
    quantity NUMERIC(20, 8) NOT NULL,
    price NUMERIC(20, 8),
    status TEXT NOT NULL CHECK (status IN ('PENDING', 'FILLED', 'PARTIALLY_FILLED', 'CANCELLED', 'REJECTED')),
    binance_order_id TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    filled_at TIMESTAMP WITH TIME ZONE,
    metadata JSONB
);

CREATE INDEX IF NOT EXISTS idx_orders_symbol ON orders(symbol);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_created_at ON orders(created_at);

-- Audit log table
CREATE TABLE IF NOT EXISTS audit_log (
    id SERIAL PRIMARY KEY,
    event_type TEXT NOT NULL,
    entity_type TEXT,
    entity_id INTEGER,
    old_values JSONB,
    new_values JSONB,
    user_id TEXT,
    timestamp TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    metadata JSONB
);

CREATE INDEX IF NOT EXISTS idx_audit_log_event_type ON audit_log(event_type);
CREATE INDEX IF NOT EXISTS idx_audit_log_entity ON audit_log(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_audit_log_timestamp ON audit_log(timestamp);

-- Function to update updated_at timestamp
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Triggers for updated_at
CREATE TRIGGER update_strategies_updated_at BEFORE UPDATE ON strategies
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_orders_updated_at BEFORE UPDATE ON orders
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

-- Insert default strategy
INSERT INTO strategies (name, description, parameters)
VALUES (
    'default',
    'Default trading strategy',
    '{"risk_per_trade": 0.02, "max_positions": 5}'::jsonb
) ON CONFLICT (name) DO NOTHING;
