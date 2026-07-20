-- =============================================================================
-- FundedFrens Telegram Bot — Database Migration
-- Run this in your Supabase SQL Editor ONCE.
--
-- This migration is ADDITIVE only. It does NOT modify any existing table
-- structure. It adds one column to 'profiles' and creates three new tables
-- (bot_settings, positions, trades) that the bot needs.
--
-- Safe to run after both migration.sql and migration_phase2.sql.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- 1. Add telegram_username to profiles
--    (website schema already has: telegram_link_code, telegram_id TEXT,
--     telegram_linked BOOLEAN — do NOT re-add those)
-- ---------------------------------------------------------------------------

ALTER TABLE public.profiles
  ADD COLUMN IF NOT EXISTS telegram_username TEXT;

CREATE INDEX IF NOT EXISTS idx_profiles_telegram_id
  ON public.profiles (telegram_id)
  WHERE telegram_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 2. bot_settings — per-user trading defaults
--    user_id is UUID referencing profiles(id) = auth.users.id
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.bot_settings (
  id                    SERIAL PRIMARY KEY,
  user_id               UUID NOT NULL UNIQUE REFERENCES public.profiles(id) ON DELETE CASCADE,
  default_buy_sol       NUMERIC(10, 4) DEFAULT 0.1   NOT NULL,
  default_sl_pct        NUMERIC(5,  2) DEFAULT 20.0  NOT NULL,
  default_tp_pct        NUMERIC(5,  2) DEFAULT 50.0  NOT NULL,
  default_auto_sell_pct NUMERIC(5,  2),               -- NULL = disabled
  created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ---------------------------------------------------------------------------
-- 3. positions — simulated open / partial / closed positions
--    challenge_id is UUID to match challenges(id) in the website schema
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.positions (
  id                    SERIAL PRIMARY KEY,
  user_id               UUID NOT NULL REFERENCES public.profiles(id)   ON DELETE CASCADE,
  challenge_id          UUID NOT NULL REFERENCES public.challenges(id)  ON DELETE CASCADE,

  token_address         TEXT NOT NULL,
  token_symbol          TEXT NOT NULL,
  token_name            TEXT,
  token_logo_url        TEXT,

  -- Size & pricing at entry
  amount_sol_invested   NUMERIC(18,  9) NOT NULL,
  entry_price_sol       NUMERIC(24, 12) NOT NULL,
  entry_market_cap_usd  NUMERIC(20,  2),
  highest_price_sol     NUMERIC(24, 12),   -- high-water mark for trailing stop

  -- Risk parameters (editable after opening)
  stop_loss_pct         NUMERIC(5,  2),
  take_profit_pct       NUMERIC(5,  2),
  trailing_stop_pct     NUMERIC(5,  2),
  auto_sell_pct         NUMERIC(5,  2),

  -- Lifecycle
  status                TEXT NOT NULL DEFAULT 'open',
  -- 'open' | 'closed' | 'partial' (partial immediately transitions back to open)
  opened_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  closed_at             TIMESTAMPTZ,
  updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_positions_user_status
  ON public.positions (user_id, status);

CREATE INDEX IF NOT EXISTS idx_positions_token_open
  ON public.positions (token_address)
  WHERE status = 'open';

-- ---------------------------------------------------------------------------
-- 4. trades — individual buy / sell records
--    Every bot trade (buy or sell) is recorded here.
--    The website analytics layer reads this table.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.trades (
  id               SERIAL PRIMARY KEY,
  user_id          UUID NOT NULL REFERENCES public.profiles(id)   ON DELETE CASCADE,
  challenge_id     UUID           REFERENCES public.challenges(id) ON DELETE SET NULL,
  position_id      INTEGER        REFERENCES public.positions(id)  ON DELETE SET NULL,

  token_address    TEXT NOT NULL,
  token_symbol     TEXT NOT NULL,
  token_name       TEXT,

  side             TEXT NOT NULL,        -- 'buy' | 'sell'
  amount_sol       NUMERIC(18, 9) NOT NULL,   -- SOL invested (buy) or received (sell)
  entry_price_sol  NUMERIC(24, 12),
  exit_price_sol   NUMERIC(24, 12),
  market_cap_usd   NUMERIC(20,  2),

  -- Populated on sell trades
  pnl_sol          NUMERIC(18,  9),
  pnl_pct          NUMERIC(10,  4),
  sell_pct         NUMERIC(5,   2),   -- % of position sold

  -- What triggered this trade
  trigger          TEXT NOT NULL DEFAULT 'manual',
  -- 'manual' | 'stop_loss' | 'take_profit' | 'trailing_stop' | 'auto_sell'

  created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_trades_user_created
  ON public.trades (user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_trades_challenge
  ON public.trades (challenge_id)
  WHERE challenge_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_trades_position
  ON public.trades (position_id)
  WHERE position_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 5. Row Level Security
--    The bot uses the service-role key which bypasses RLS entirely.
--    These policies protect the tables if they are ever accessed with
--    the anon/user key (e.g., from the website in a future phase).
-- ---------------------------------------------------------------------------

ALTER TABLE public.bot_settings ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.positions    ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.trades       ENABLE ROW LEVEL SECURITY;

-- bot_settings
DROP POLICY IF EXISTS "Users can view own bot_settings" ON public.bot_settings;
CREATE POLICY "Users can view own bot_settings"
  ON public.bot_settings FOR SELECT
  USING (auth.uid() = user_id);

-- positions
DROP POLICY IF EXISTS "Users can view own positions" ON public.positions;
CREATE POLICY "Users can view own positions"
  ON public.positions FOR SELECT
  USING (auth.uid() = user_id);

-- trades
DROP POLICY IF EXISTS "Users can view own trades" ON public.trades;
CREATE POLICY "Users can view own trades"
  ON public.trades FOR SELECT
  USING (auth.uid() = user_id);

-- =============================================================================
-- Verification queries — run after migration to confirm success:
--
-- SELECT column_name FROM information_schema.columns
--   WHERE table_name = 'profiles'
--   AND column_name IN ('telegram_id', 'telegram_link_code', 'telegram_linked',
--                       'telegram_username');
--
-- SELECT table_name FROM information_schema.tables
--   WHERE table_name IN ('bot_settings', 'positions', 'trades');
-- =============================================================================
