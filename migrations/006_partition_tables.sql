-- NOTE: Partitioning requires a full table migration.
-- This script creates partitioned copies and swaps them in.
-- Run during a maintenance window when the system is STOPPED.
-- Migration: 006_partition_tables.sql

-- ─── market_data: partition by range on timestamp (monthly) ──────────────────

ALTER TABLE market_data RENAME TO market_data_old;

CREATE TABLE market_data (
    id          SERIAL,
    symbol      VARCHAR(20)  NOT NULL,
    data_type   VARCHAR(50)  NOT NULL,
    data        JSONB        NOT NULL,
    timestamp   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    expires_at  TIMESTAMPTZ
) PARTITION BY RANGE (timestamp);

-- Create monthly partitions for the current and next 3 months
DO $$
DECLARE
    cur_month DATE := date_trunc('month', NOW());
    i INT;
BEGIN
    FOR i IN 0..3 LOOP
        EXECUTE format(
            'CREATE TABLE IF NOT EXISTS market_data_%s PARTITION OF market_data
             FOR VALUES FROM (%L) TO (%L)',
            to_char(cur_month + (i || ' months')::INTERVAL, 'YYYY_MM'),
            cur_month + (i || ' months')::INTERVAL,
            cur_month + ((i+1) || ' months')::INTERVAL
        );
    END LOOP;
END $$;

-- Indexes on the partitioned table
CREATE INDEX IF NOT EXISTS idx_md_part_symbol_type_ts
    ON market_data(symbol, data_type, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_md_part_expires
    ON market_data(expires_at) WHERE expires_at IS NOT NULL;

-- Copy data
INSERT INTO market_data SELECT * FROM market_data_old;

-- ─── Automatic partition creation function ────────────────────────────────────
CREATE OR REPLACE FUNCTION create_market_data_partition_for_month(target_month DATE)
RETURNS VOID LANGUAGE plpgsql AS $$
DECLARE
    part_name TEXT := 'market_data_' || to_char(target_month, 'YYYY_MM');
    start_val TIMESTAMPTZ := date_trunc('month', target_month);
    end_val   TIMESTAMPTZ := start_val + INTERVAL '1 month';
BEGIN
    EXECUTE format(
        'CREATE TABLE IF NOT EXISTS %I PARTITION OF market_data
         FOR VALUES FROM (%L) TO (%L)',
        part_name, start_val, end_val
    );
END $$;

-- ─── Maintenance: drop partitions older than 3 months ────────────────────────
CREATE OR REPLACE FUNCTION drop_old_market_data_partitions()
RETURNS VOID LANGUAGE plpgsql AS $$
DECLARE
    cutoff TEXT := to_char(NOW() - INTERVAL '3 months', 'YYYY_MM');
    part_name TEXT;
BEGIN
    FOR part_name IN
        SELECT tablename FROM pg_tables
        WHERE schemaname = 'public' AND tablename LIKE 'market_data_%'
          AND tablename < 'market_data_' || cutoff
    LOOP
        EXECUTE 'DROP TABLE IF EXISTS ' || quote_ident(part_name);
        RAISE NOTICE 'Dropped partition %', part_name;
    END LOOP;
END $$;
