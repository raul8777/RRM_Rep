-- Add canonical lineage columns to trades_closed for end-to-end traceability

ALTER TABLE trades_closed
ADD COLUMN IF NOT EXISTS position_id TEXT,
ADD COLUMN IF NOT EXISTS entry_order_id TEXT,
ADD COLUMN IF NOT EXISTS exit_order_id TEXT,
ADD COLUMN IF NOT EXISTS close_group_id TEXT;

CREATE INDEX IF NOT EXISTS idx_trades_closed_position_id ON trades_closed(position_id);
CREATE INDEX IF NOT EXISTS idx_trades_closed_entry_order_id ON trades_closed(entry_order_id);
CREATE INDEX IF NOT EXISTS idx_trades_closed_exit_order_id ON trades_closed(exit_order_id);
CREATE INDEX IF NOT EXISTS idx_trades_closed_close_group_id ON trades_closed(close_group_id);