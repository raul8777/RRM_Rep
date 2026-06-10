-- Add system_state table for manual control (pause/stop/start)
-- Migration: 002_add_system_state.sql

CREATE TABLE IF NOT EXISTS system_state (
    id SERIAL PRIMARY KEY,
    state TEXT NOT NULL CHECK (state IN ('RUNNING', 'PAUSED', 'STOPPED')),
    changed_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    changed_by TEXT NOT NULL,
    reason TEXT,
    metadata JSONB
);

-- Index for quick queries
CREATE INDEX IF NOT EXISTS idx_system_state_changed_at ON system_state(changed_at DESC);

-- Insert initial state
INSERT INTO system_state (state, changed_by, reason)
VALUES ('STOPPED', 'system', 'Initial state')
ON CONFLICT DO NOTHING;
