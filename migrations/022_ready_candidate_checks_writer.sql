-- Migration 022: writer provenance + lifecycle check_type for ready_candidate_checks
--
-- Closes the audit gap where several state-changing writes to ready_candidates
-- (risk-queue selection, max-age/max-cycles expiry, open confirmation, regime-change
-- discard) had no corresponding row in ready_candidate_checks. Adds a `writer` column
-- so every check can be attributed to the process that generated it (main cycle,
-- intra-cycle watcher, risk selector, expiry sweep, regime guard), and extends the
-- check_type constraint to allow 'lifecycle' for these non-price-check events.

ALTER TABLE ready_candidate_checks
    ADD COLUMN IF NOT EXISTS writer TEXT;

ALTER TABLE ready_candidate_checks
    DROP CONSTRAINT IF EXISTS ready_candidate_checks_check_type_check;

ALTER TABLE ready_candidate_checks
    ADD CONSTRAINT ready_candidate_checks_check_type_check
    CHECK (check_type IN ('intra', 'inter', 'manual', 'lifecycle'));

CREATE INDEX IF NOT EXISTS idx_ready_checks_writer
    ON ready_candidate_checks(writer, checked_at DESC);
