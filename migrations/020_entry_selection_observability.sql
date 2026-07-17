CREATE TABLE IF NOT EXISTS entry_selection_cycles (
    cycle_id TEXT PRIMARY KEY,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    regime_mode TEXT,
    profile_label TEXT,
    candidate_count INTEGER NOT NULL DEFAULT 0,
    selected_count INTEGER NOT NULL DEFAULT 0,
    executed_count INTEGER NOT NULL DEFAULT 0,
    summary JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS entry_selection_candidates (
    cycle_id TEXT NOT NULL REFERENCES entry_selection_cycles(cycle_id) ON DELETE CASCADE,
    symbol TEXT NOT NULL,
    trade_type TEXT NOT NULL DEFAULT 'spot',
    stage TEXT NOT NULL,
    decision TEXT NOT NULL DEFAULT 'pending',
    reason TEXT,
    data JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (cycle_id, symbol)
);

CREATE INDEX IF NOT EXISTS idx_entry_selection_cycles_started_at
    ON entry_selection_cycles(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_entry_selection_candidates_cycle_id
    ON entry_selection_candidates(cycle_id);
CREATE INDEX IF NOT EXISTS idx_entry_selection_candidates_decision
    ON entry_selection_candidates(decision, updated_at DESC);
