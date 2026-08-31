-- =============================================================================
-- FundedFrens Bot — Migration 003: Backend Sync Columns
-- Run AFTER 001_bot_tables.sql
-- Adds columns that keep the website's challenges table in sync with bot data.
-- =============================================================================

-- Sync columns on challenges (written by the bot after every trade)
ALTER TABLE public.challenges
  ADD COLUMN IF NOT EXISTS is_funded         BOOLEAN DEFAULT FALSE NOT NULL,
  ADD COLUMN IF NOT EXISTS buying_power_sol  NUMERIC(18, 9),
  ADD COLUMN IF NOT EXISTS realized_pnl_sol  NUMERIC(18, 9) DEFAULT 0,
  ADD COLUMN IF NOT EXISTS open_positions    INTEGER DEFAULT 0 NOT NULL;

-- open_positions already exists in the website's phase-2 schema but may be
-- missing in some deployments — the IF NOT EXISTS guard makes this idempotent.
