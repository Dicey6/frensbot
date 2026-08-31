-- =============================================================================
-- HoodFund Bot — Migration 004: Stable Challenge Balance & Quick Sell
-- Run AFTER migrations 001 and 003.
-- ADDITIVE ONLY — no existing columns are modified.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- 1. Lock the starting SOL balance on challenges.
--    Set ONCE when the bot first accesses the challenge and never changed,
--    even if the SOL/USD price moves.
--    Backfilled from orders.sol_price_usd where an order exists.
-- ---------------------------------------------------------------------------

ALTER TABLE public.challenges
  ADD COLUMN IF NOT EXISTS start_balance_sol NUMERIC(18, 9);

-- Backfill from linked orders (challenge was paid at a specific SOL price)
UPDATE public.challenges c
SET start_balance_sol = ROUND(
    CASE c.challenge_plan
      WHEN 'starter'       THEN 350.0
      WHEN 'advanced'      THEN 1100.0
      WHEN 'professional'  THEN 3500.0
      ELSE 350.0
    END / NULLIF(o.sol_price_usd, 0),
    9
)
FROM public.orders o
WHERE o.id = c.order_id
  AND o.sol_price_usd IS NOT NULL
  AND o.sol_price_usd > 0
  AND c.start_balance_sol IS NULL;

-- ---------------------------------------------------------------------------
-- 2. Add quick-sell preset columns to bot_settings.
--    Defaults: 25 / 50 / 100 %
-- ---------------------------------------------------------------------------

ALTER TABLE public.bot_settings
  ADD COLUMN IF NOT EXISTS quick_sell_1 NUMERIC(5, 2) DEFAULT 25.0  NOT NULL,
  ADD COLUMN IF NOT EXISTS quick_sell_2 NUMERIC(5, 2) DEFAULT 50.0  NOT NULL,
  ADD COLUMN IF NOT EXISTS quick_sell_3 NUMERIC(5, 2) DEFAULT 100.0 NOT NULL;

-- ---------------------------------------------------------------------------
-- 3. Nullify auto_sell — feature removed.
--    Columns kept in schema for backwards compatibility.
-- ---------------------------------------------------------------------------

UPDATE public.bot_settings SET default_auto_sell_pct = NULL
  WHERE default_auto_sell_pct IS NOT NULL
    AND EXISTS (
      SELECT 1 FROM information_schema.columns
      WHERE table_name = 'bot_settings' AND column_name = 'default_auto_sell_pct'
    );
