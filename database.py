"""
database.py — Supabase data-access layer for the FundedFrens bot.

Schema contract (must match live website schema):
  profiles.id               UUID PK = auth.users.id
  profiles.telegram_link_code  TEXT (permanent code, format TG-XXXXXXXXXX)
  profiles.telegram_linked     BOOLEAN
  profiles.telegram_id         TEXT  (stores Telegram int as str)
  profiles.telegram_username   TEXT  (added by bot migration)
  challenges                table (not user_challenges / challenge_plans)
  positions, trades, bot_settings  (added by bot migration.sql)

The service-role key bypasses RLS — no user JWT needed.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

import httpx
from supabase import AsyncClient, create_async_client

import config
from config import PLAN_USD, TRADING

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Singleton async client
# ---------------------------------------------------------------------------

_client: AsyncClient | None = None
_client_lock = asyncio.Lock()


async def get_client() -> AsyncClient:
    global _client
    if _client is None:
        async with _client_lock:
            if _client is None:
                _client = await create_async_client(
                    config.SUPABASE_URL,
                    config.SUPABASE_SERVICE_KEY,
                )
    return _client


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _fetch_sol_price() -> float:
    """
    Return current SOL/USD price from CoinGecko.
    Falls back to 150.0 if the request fails.
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as http:
            r = await http.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": "solana", "vs_currencies": "usd"},
            )
            r.raise_for_status()
            return float(r.json()["solana"]["usd"])
    except Exception:
        log.warning("SOL price fetch failed — using $150.00 fallback")
        return 150.0


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------

async def get_profile_by_telegram_id(telegram_id: int) -> dict | None:
    """
    Look up a profile by Telegram numeric user ID.
    The website column 'telegram_id' is TEXT, so we compare as str.
    """
    db = await get_client()
    res = await (
        db.table("profiles")
        .select("*")
        .eq("telegram_id", str(telegram_id))
        .limit(1)
        .execute()
    )
    rows = res.data or []
    return rows[0] if rows else None


async def get_profile_by_link_code(code: str) -> dict | None:
    """
    Look up a profile by its telegram_link_code (format: TG-XXXXXXXXXX).
    Returns None if the code is not found OR if the account is already linked
    (prevents re-linking to a different Telegram account).
    No expiry check — the code is permanent per Q1 decision.
    """
    db = await get_client()
    res = await (
        db.table("profiles")
        .select("*")
        .eq("telegram_link_code", code.strip().upper())
        .limit(1)
        .execute()
    )
    rows = res.data or []
    if not rows:
        return None
    profile = rows[0]
    # Already linked → reject (silently return None)
    if profile.get("telegram_linked"):
        return None
    return profile


async def link_telegram(
    profile_id: str,
    telegram_id: int,
    telegram_username: str | None,
) -> bool:
    """
    Persist the Telegram link on the profile.
    - Stores telegram_id as str (website column is TEXT)
    - Sets telegram_linked = True
    - Does NOT clear telegram_link_code (Q1: stays intact as permanent ref)
    """
    db = await get_client()
    try:
        await (
            db.table("profiles")
            .update({
                "telegram_id":       str(telegram_id),
                "telegram_linked":   True,
                "telegram_username": telegram_username,
                "updated_at":        _now(),
            })
            .eq("id", profile_id)
            .execute()
        )
        return True
    except Exception as e:
        log.error("link_telegram failed for profile %s: %s", profile_id, e)
        return False


# ---------------------------------------------------------------------------
# Challenges
# ---------------------------------------------------------------------------

async def get_active_challenge(user_id: str) -> dict | None:
    """
    Return the user's active challenge from the 'challenges' table.
    user_id is profiles.id (UUID).
    """
    db = await get_client()
    res = await (
        db.table("challenges")
        .select("*")
        .eq("user_id", user_id)
        .eq("status", "active")
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    rows = res.data or []
    return rows[0] if rows else None


# ---------------------------------------------------------------------------
# Account summary (home screen)
# ---------------------------------------------------------------------------

async def get_account_summary(user_id: str, challenge: dict) -> dict:
    """
    Compute the trading account summary for the home screen.

    Demo balance = challenge plan USD value ÷ live SOL price.
    Recalculated on every call (Q2 decision: follow the price of SOL).
    """
    plan_name = (challenge.get("challenge_plan") or "starter").lower()
    plan_usd  = PLAN_USD.get(plan_name, 350.0)

    sol_price     = await _fetch_sol_price()
    start_balance = plan_usd / sol_price if sol_price > 0 else 0.0

    db = await get_client()

    # Realized PnL from all sell trades for this challenge
    trades_res = await (
        db.table("trades")
        .select("pnl_sol")
        .eq("user_id", user_id)
        .eq("challenge_id", challenge["id"])
        .eq("side", "sell")
        .execute()
    )
    realized_pnl = sum(
        float(t.get("pnl_sol") or 0)
        for t in (trades_res.data or [])
    )

    # SOL locked in open positions
    pos_res = await (
        db.table("positions")
        .select("amount_sol_invested")
        .eq("user_id", user_id)
        .eq("status", "open")
        .execute()
    )
    invested_sol = sum(
        float(p.get("amount_sol_invested") or 0)
        for p in (pos_res.data or [])
    )

    available_sol = max(0.0, start_balance + realized_pnl - invested_sol)
    total_equity  = start_balance + realized_pnl
    pnl_pct       = (realized_pnl / start_balance * 100) if start_balance else 0.0
    drawdown_pct  = max(0.0, -realized_pnl / start_balance * 100) if start_balance else 0.0

    return {
        "plan_name":     plan_name.title(),
        "plan_usd":      plan_usd,
        "sol_price":     sol_price,
        "start_balance": start_balance,
        "available_sol": available_sol,
        "invested_sol":  invested_sol,
        "total_equity":  total_equity,
        "realized_pnl":  realized_pnl,
        "pnl_pct":       pnl_pct,
        "drawdown_pct":  drawdown_pct,
    }


# ---------------------------------------------------------------------------
# Bot settings
# ---------------------------------------------------------------------------

async def get_bot_settings(user_id: str) -> dict:
    """Return the user's bot_settings row, inserting defaults if absent."""
    db = await get_client()
    res = await (
        db.table("bot_settings")
        .select("*")
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    )
    rows = res.data or []
    if rows:
        return rows[0]

    # First-time user — insert defaults then return them
    defaults: dict[str, Any] = {
        "user_id":               user_id,
        "default_buy_sol":       TRADING.default_buy_sol,
        "default_sl_pct":        TRADING.default_sl_pct,
        "default_tp_pct":        TRADING.default_tp_pct,
        "default_auto_sell_pct": TRADING.default_auto_sell_pct,
    }
    insert_res = await db.table("bot_settings").insert(defaults).execute()
    return (insert_res.data or [defaults])[0]


async def upsert_bot_settings(user_id: str, **updates: Any) -> None:
    db = await get_client()
    payload = {"user_id": user_id, "updated_at": _now(), **updates}
    await (
        db.table("bot_settings")
        .upsert(payload, on_conflict="user_id")
        .execute()
    )


# ---------------------------------------------------------------------------
# Positions
# ---------------------------------------------------------------------------

async def get_open_positions(user_id: str) -> list[dict]:
    db = await get_client()
    res = await (
        db.table("positions")
        .select("*")
        .eq("user_id", user_id)
        .eq("status", "open")
        .order("opened_at", desc=False)
        .execute()
    )
    return res.data or []


async def get_position(position_id: int) -> dict | None:
    db = await get_client()
    res = await (
        db.table("positions")
        .select("*")
        .eq("id", position_id)
        .limit(1)
        .execute()
    )
    rows = res.data or []
    return rows[0] if rows else None


async def get_all_open_positions() -> list[dict]:
    """Used by the background monitor across all users."""
    db = await get_client()
    res = await (
        db.table("positions")
        .select("*")
        .eq("status", "open")
        .execute()
    )
    return res.data or []


async def create_position(
    *,
    user_id: str,
    challenge_id: str,
    token_address: str,
    token_symbol: str,
    token_name: str | None,
    token_logo_url: str | None,
    amount_sol_invested: float,
    entry_price_sol: float,
    entry_market_cap_usd: float | None,
    stop_loss_pct: float | None,
    take_profit_pct: float | None,
    auto_sell_pct: float | None,
) -> dict:
    db = await get_client()
    row = {
        "user_id":               user_id,
        "challenge_id":          challenge_id,
        "token_address":         token_address,
        "token_symbol":          token_symbol,
        "token_name":            token_name,
        "token_logo_url":        token_logo_url,
        "amount_sol_invested":   amount_sol_invested,
        "entry_price_sol":       entry_price_sol,
        "highest_price_sol":     entry_price_sol,   # high-water mark for trailing stop
        "entry_market_cap_usd":  entry_market_cap_usd,
        "stop_loss_pct":         stop_loss_pct,
        "take_profit_pct":       take_profit_pct,
        "auto_sell_pct":         auto_sell_pct,
        "status":                "open",
    }
    res = await db.table("positions").insert(row).execute()
    return res.data[0]


async def update_position_high(position_id: int, current_price_sol: float) -> None:
    """Advance the high-water mark if the price has risen (used for trailing stops)."""
    db = await get_client()
    pos = await get_position(position_id)
    if pos and current_price_sol > float(pos.get("highest_price_sol") or 0):
        await (
            db.table("positions")
            .update({"highest_price_sol": current_price_sol, "updated_at": _now()})
            .eq("id", position_id)
            .execute()
        )


async def close_position(
    position_id: int,
    exit_price_sol: float,
    sell_pct: float = 100.0,
) -> dict:
    """
    Simulate a sell of `sell_pct`% of a position.
    Returns a result dict containing pnl_sol, pnl_pct, etc.
    Closes fully if sell_pct >= 100, otherwise reduces the invested amount.
    """
    db = await get_client()
    pos = await get_position(position_id)
    if pos is None:
        raise ValueError(f"Position {position_id} not found")

    entry_price  = float(pos["entry_price_sol"])
    invested_sol = float(pos["amount_sol_invested"])
    frac         = min(sell_pct / 100.0, 1.0)

    # Simulated token quantity model: tokens = invested / entry_price
    simulated_tokens = invested_sol / entry_price if entry_price > 0 else 0
    sold_tokens      = simulated_tokens * frac
    received_sol     = sold_tokens * exit_price_sol
    cost_basis       = invested_sol * frac

    pnl_sol = received_sol - cost_basis
    pnl_pct = (pnl_sol / cost_basis * 100) if cost_basis > 0 else 0.0

    is_full_close  = sell_pct >= 99.99
    new_status     = "closed" if is_full_close else "open"
    new_invested   = invested_sol * (1 - frac) if not is_full_close else 0.0

    await (
        db.table("positions")
        .update({
            "status":              new_status,
            "amount_sol_invested": new_invested,
            "closed_at":          _now() if is_full_close else None,
            "updated_at":         _now(),
        })
        .eq("id", position_id)
        .execute()
    )

    return {
        "position_id":   position_id,
        "user_id":       pos["user_id"],
        "challenge_id":  pos["challenge_id"],
        "token_symbol":  pos["token_symbol"],
        "token_name":    pos.get("token_name"),
        "token_address": pos["token_address"],
        "entry_price":   entry_price,
        "exit_price":    exit_price_sol,
        "invested_sol":  cost_basis,
        "received_sol":  received_sol,
        "pnl_sol":       pnl_sol,
        "pnl_pct":       pnl_pct,
        "sell_pct":      sell_pct,
    }


# ---------------------------------------------------------------------------
# Trades
# ---------------------------------------------------------------------------

async def record_trade(
    *,
    user_id: str,
    challenge_id: str | None,
    position_id: int | None,
    token_address: str,
    token_symbol: str,
    token_name: str | None,
    side: str,                       # 'buy' | 'sell'
    amount_sol: float,
    entry_price_sol: float | None,
    exit_price_sol: float | None,
    market_cap_usd: float | None,
    pnl_sol: float | None,
    pnl_pct: float | None,
    sell_pct: float | None,
    trigger: str = "manual",
) -> dict:
    db = await get_client()
    row = {
        "user_id":         user_id,
        "challenge_id":    challenge_id,
        "position_id":     position_id,
        "token_address":   token_address,
        "token_symbol":    token_symbol,
        "token_name":      token_name,
        "side":            side,
        "amount_sol":      amount_sol,
        "entry_price_sol": entry_price_sol,
        "exit_price_sol":  exit_price_sol,
        "market_cap_usd":  market_cap_usd,
        "pnl_sol":         pnl_sol,
        "pnl_pct":         pnl_pct,
        "sell_pct":        sell_pct,
        "trigger":         trigger,
    }
    res = await db.table("trades").insert(row).execute()
    return res.data[0]


async def get_trades(user_id: str, limit: int = 20) -> list[dict]:
    db = await get_client()
    res = await (
        db.table("trades")
        .select("*")
        .eq("user_id", user_id)
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return res.data or []


# ---------------------------------------------------------------------------
# Challenge stats (Q3: update after every buy/sell so website stays in sync)
# ---------------------------------------------------------------------------

async def update_challenge_stats(user_id: str, challenge_id: str) -> None:
    """
    Recompute and persist aggregated stats on the challenges row.
    Called after every buy or sell from trading.py.

    Updates: open_positions, trading_days, win_rate, drawdown, challenge_progress.
    """
    db = await get_client()

    # Open positions count + invested amount
    pos_res = await (
        db.table("positions")
        .select("id, amount_sol_invested", count="exact")
        .eq("user_id", user_id)
        .eq("status", "open")
        .execute()
    )
    open_count   = pos_res.count or 0

    # Sell trades → realized PnL and win rate
    sell_res = await (
        db.table("trades")
        .select("pnl_sol")
        .eq("user_id", user_id)
        .eq("challenge_id", challenge_id)
        .eq("side", "sell")
        .execute()
    )
    sell_trades  = sell_res.data or []
    total_sells  = len(sell_trades)
    winners      = sum(1 for t in sell_trades if float(t.get("pnl_sol") or 0) > 0)
    win_rate     = round(winners / total_sells * 100, 2) if total_sells else 0.0
    realized_pnl = sum(float(t.get("pnl_sol") or 0) for t in sell_trades)

    # Trading days: count of distinct calendar days with any trade
    all_trades_res = await (
        db.table("trades")
        .select("created_at")
        .eq("user_id", user_id)
        .eq("challenge_id", challenge_id)
        .execute()
    )
    trading_days = len({
        t["created_at"][:10]
        for t in (all_trades_res.data or [])
        if t.get("created_at")
    })

    # Start balance in SOL for % calculations
    ch_res = await (
        db.table("challenges")
        .select("challenge_plan")
        .eq("id", challenge_id)
        .single()
        .execute()
    )
    plan_name   = ((ch_res.data or {}).get("challenge_plan") or "starter").lower()
    plan_usd    = PLAN_USD.get(plan_name, 350.0)
    sol_price   = await _fetch_sol_price()
    start_bal   = plan_usd / sol_price if sol_price > 0 else 0.0

    challenge_progress = round(
        max(0.0, realized_pnl / start_bal * 100) if start_bal else 0.0, 2
    )
    # Drawdown: decline from start balance (realized only; unrealized handled by monitor)
    drawdown = round(
        max(0.0, -realized_pnl / start_bal * 100) if start_bal else 0.0, 2
    )

    await (
        db.table("challenges")
        .update({
            "open_positions":     open_count,
            "win_rate":           win_rate,
            "drawdown":           drawdown,
            "challenge_progress": challenge_progress,
            "trading_days":       trading_days,
            "updated_at":         _now(),
        })
        .eq("id", challenge_id)
        .execute()
    )
