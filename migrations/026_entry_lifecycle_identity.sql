ALTER TABLE IF EXISTS trades_open
    ADD COLUMN IF NOT EXISTS position_id TEXT,
    ADD COLUMN IF NOT EXISTS thesis_id TEXT,
    ADD COLUMN IF NOT EXISTS close_group_id TEXT,
    ADD COLUMN IF NOT EXISTS entry_order_id TEXT,
    ADD COLUMN IF NOT EXISTS protection_order_list_id TEXT,
    ADD COLUMN IF NOT EXISTS margin_loan_transaction_id TEXT,
    ADD COLUMN IF NOT EXISTS margin_borrowed_qty NUMERIC(30, 12);

ALTER TABLE IF EXISTS trades_closed
    ADD COLUMN IF NOT EXISTS thesis_id TEXT,
    ADD COLUMN IF NOT EXISTS margin_loan_transaction_id TEXT,
    ADD COLUMN IF NOT EXISTS margin_repay_transaction_id TEXT,
    ADD COLUMN IF NOT EXISTS margin_interest_qty NUMERIC(30, 12),
    ADD COLUMN IF NOT EXISTS idempotency_key TEXT;

ALTER TABLE IF EXISTS trades_audit
    DROP CONSTRAINT IF EXISTS trades_audit_trade_type_check;

ALTER TABLE IF EXISTS trades_audit
    ADD CONSTRAINT trades_audit_trade_type_check
    CHECK (trade_type IN ('spot', 'spot_margin', 'futures', 'system'));

CREATE UNIQUE INDEX IF NOT EXISTS uq_trades_open_position_id
    ON trades_open(position_id) WHERE position_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_trades_open_thesis_id ON trades_open(thesis_id);
CREATE INDEX IF NOT EXISTS idx_trades_open_close_group_id ON trades_open(close_group_id);
CREATE INDEX IF NOT EXISTS idx_trades_closed_thesis_id ON trades_closed(thesis_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_trades_closed_idempotency_key
    ON trades_closed(idempotency_key) WHERE idempotency_key IS NOT NULL;