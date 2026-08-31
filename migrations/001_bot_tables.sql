-- =============================================================================
-- HoodFund Bot — Migration 001: Core Bot Tables
-- Run this in your Supabase SQL Editor before running the bot.
-- =============================================================================

-- Add telegram_username to profiles (populated when user links Telegram)
ALTER TABLE public.profiles
  ADD COLUMN IF NOT EXISTS telegram_username TEXT;

-- ---------------------------------------------------------------------------
-- bot_settings — per-user trading preferences
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.bot_settings (
  id                         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id                    UUID UNIQUE NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,

  -- Quick-buy preset amounts (SOL)
  quick_buy_1                NUMERIC(12, 6) DEFAULT 0.1  NOT NULL,
  quick_buy_2                NUMERIC(12, 6) DEFAULT 0.5  NOT NULL,
  quick_buy_3                NUMERIC(12, 6) DEFAULT 1.0  NOT NULL,

  -- Default risk settings (NULL = not set, never applied)
  default_buy_sol            NUMERIC(12, 6),
  default_sl_pct             NUMERIC(5, 2),
  default_tp_pct             NUMERIC(5, 2),
  default_trailing_stop_pct  NUMERIC(5, 2),

  created_at  TIMESTAMPTZ DEFAULT NOW() NOT NULL,
  updated_at  TIMESTAMPTZ DEFAULT NOW() NOT NULL
);

-- ---------------------------------------------------------------------------
-- positions — open / closed simulated positions
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.positions (
  id                    BIGSERIAL PRIMARY KEY,
  user_id               UUID NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  challenge_id          UUID NOT NULL REFERENCES public.challenges(id) ON DELETE CASCADE,

  token_address         TEXT NOT NULL,
  token_symbol          TEXT NOT NULL,
  token_name            TEXT,
  token_logo_url        TEXT,

  amount_sol_invested   NUMERIC(18, 9) NOT NULL,
  entry_price_sol       NUMERIC(18, 12) NOT NULL,
  highest_price_sol     NUMERIC(18, 12),
  entry_market_cap_usd  NUMERIC(18, 2),

  stop_loss_pct         NUMERIC(5, 2),
  take_profit_pct       NUMERIC(5, 2),
  trailing_stop_pct     NUMERIC(5, 2),

  status                TEXT DEFAULT 'open' NOT NULL
    CHECK (status IN ('open', 'closed')),

  opened_at   TIMESTAMPTZ DEFAULT NOW() NOT NULL,
  closed_at   TIMESTAMPTZ,
  created_at  TIMESTAMPTZ DEFAULT NOW() NOT NULL,
  updated_at  TIMESTAMPTZ DEFAULT NOW() NOT NULL
);

-- ---------------------------------------------------------------------------
-- trades — individual buy / sell records
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.trades (
  id               BIGSERIAL PRIMARY KEY,
  user_id          UUID NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  challenge_id     UUID REFERENCES public.challenges(id) ON DELETE SET NULL,
  position_id      BIGINT REFERENCES public.positions(id) ON DELETE SET NULL,

  token_address    TEXT NOT NULL,
  token_symbol     TEXT NOT NULL,
  token_name       TEXT,

  side             TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
  amount_sol       NUMERIC(18, 9) NOT NULL,
  entry_price_sol  NUMERIC(18, 12),
  exit_price_sol   NUMERIC(18, 12),
  market_cap_usd   NUMERIC(18, 2),
  pnl_sol          NUMERIC(18, 9),
  pnl_pct          NUMERIC(10, 4),
  sell_pct         NUMERIC(5, 2),
  trigger          TEXT DEFAULT 'manual',

  -- Analytics columns (populated on all trades)
  exit_market_cap_usd  NUMERIC(18, 2),
  liquidity_usd        NUMERIC(18, 2),
  volume_24h_usd       NUMERIC(18, 2),
  price_impact_pct     NUMERIC(10, 4),
  hold_time_seconds    INTEGER,
  entry_time           TIMESTAMPTZ,
  exit_time            TIMESTAMPTZ,
  stop_loss_pct        NUMERIC(5, 2),
  take_profit_pct      NUMERIC(5, 2),
  trailing_stop_pct    NUMERIC(5, 2),

  created_at  TIMESTAMPTZ DEFAULT NOW() NOT NULL
);

-- ---------------------------------------------------------------------------
-- Indexes
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_bot_settings_user_id   ON public.bot_settings(user_id);
CREATE INDEX IF NOT EXISTS idx_positions_user_id       ON public.positions(user_id);
CREATE INDEX IF NOT EXISTS idx_positions_challenge_id  ON public.positions(challenge_id);
CREATE INDEX IF NOT EXISTS idx_positions_status        ON public.positions(status);
CREATE INDEX IF NOT EXISTS idx_trades_user_id          ON public.trades(user_id);
CREATE INDEX IF NOT EXISTS idx_trades_challenge_id     ON public.trades(challenge_id);
CREATE INDEX IF NOT EXISTS idx_trades_position_id      ON public.trades(position_id);
CREATE INDEX IF NOT EXISTS idx_trades_side             ON public.trades(side);
CREATE INDEX IF NOT EXISTS idx_trades_created_at       ON public.trades(created_at DESC);

-- updated_at triggers
CREATE OR REPLACE FUNCTION public.update_updated_at()
RETURNS TRIGGER AS $$
BEGIN NEW.updated_at = NOW(); RETURN NEW; END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS bot_settings_updated_at ON public.bot_settings;
CREATE TRIGGER bot_settings_updated_at
  BEFORE UPDATE ON public.bot_settings
  FOR EACH ROW EXECUTE FUNCTION public.update_updated_at();

DROP TRIGGER IF EXISTS positions_updated_at ON public.positions;
CREATE TRIGGER positions_updated_at
  BEFORE UPDATE ON public.positions
  FOR EACH ROW EXECUTE FUNCTION public.update_updated_at();
