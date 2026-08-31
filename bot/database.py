"""
database.py — Supabase data-access layer for the HoodFund bot.

Principles:
  - challenge.start_balance_sol is locked once at first access and never recalculated.
  - Never silently enable risk features — None means Not Set.
  - Every trade is recorded with full market context for website analytics.
  - Challenge stats are recalculated and persisted after every buy/sell.
  - SOL price is cached for 60 s to reduce external API calls.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

import httpx
from supabase import AsyncClient, create_async_client

import config
from config import PLAN_USD, TRADING

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Singleton Supabase client
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
# SOL/USD price — cached for TRADING.sol_price_cache_ttl seconds
# ---------------------------------------------------------------------------

_sol_price_value: float = 150.0
_sol_price_ts: float = 0.0


async def fetch_sol_price() -> float:
    global _sol_price_value, _sol_price_ts
    now = time.monotonic()
    if now - _sol_price_ts < TRADING.sol_price_cache_ttl:
        return _sol_price_value
    try:
        async with httpx.AsyncClient(timeout=10.0) as http:
            r = await http.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": "solana", "vs_currencies": "usd"},
            )
            r.raise_for_status()
            price = float(r.json()["solana"]["usd"])
            _sol_price_value = price
            _sol_price_ts = now
            return price
    except Exception:
        log.warning("SOL price fetch failed — using cached $%.2f", _sol_price_value)
        return _sol_price_value


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------

async def get_profile_by_telegram_id(telegram_id: int) -> dict | None:
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
    return None if profile.get("telegram_linked") else profile


async def link_telegram(
    profile_id: str,
    telegram_id: int,
    telegram_username: str | None,
) -> bool:
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

# In-memory cache: challenge_id -> start_balance_sol
# Prevents start_balance from drifting with SOL price across calls in the
# same bot session.  The cache is intentionally not persisted across restarts —
# on a restart the price is re-fetched once and then locked again.
_start_balance_cache: dict[str, float] = {}


async def get_active_challenge(user_id: str) -> dict | None:
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
    if not rows:
        return None
    challenge = rows[0]
    if not challenge.get("start_balance_sol"):
        challenge = await _initialize_challenge_balance(challenge)
    return challenge


async def _initialize_challenge_balance(challenge: dict) -> dict:
    """
    Compute start_balance_sol for this challenge and return an enriched
    challenge dict.  The value is cached in _start_balance_cache by
    challenge_id so the same figure is used for the whole bot session,
    even if SOL price moves.

    We do NOT write back to the DB because the challenges table has no
    start_balance_sol column — the value is derived at runtime from the plan.
    """
    ch_id = challenge["id"]

    # Return cached value if already computed this session
    if ch_id in _start_balance_cache:
        return {**challenge, "start_balance_sol": _start_balance_cache[ch_id]}

    db_client = await get_client()
    plan_name  = (challenge.get("challenge_plan") or "starter").lower()
    plan_usd   = PLAN_USD.get(plan_name, 350.0)
    sol_price: float | None = None

    order_id = challenge.get("order_id")
    if order_id:
        try:
            ord_res = await (
                db_client.table("orders")
                .select("sol_price_usd")
                .eq("id", order_id)
                .limit(1)
                .execute()
            )
            row = (ord_res.data or [None])[0]
            if row and row.get("sol_price_usd"):
                sol_price = float(row["sol_price_usd"])
        except Exception as e:
            log.warning("Could not read order sol_price_usd: %s", e)

    if not sol_price or sol_price <= 0:
        sol_price = await fetch_sol_price()

    start_balance_sol = round(plan_usd / sol_price, 9)
    _start_balance_cache[ch_id] = start_balance_sol
    log.info(
        "Computed+cached start_balance_sol=%.4f SOL for challenge %s (plan=%s @$%.2f)",
        start_balance_sol, ch_id, plan_name, sol_price,
    )
    return {**challenge, "start_balance_sol": start_balance_sol}


# ---------------------------------------------------------------------------
# Account summary
# ---------------------------------------------------------------------------

async def get_account_summary(user_id: str, challenge: dict) -> dict:
    """
    Compute the trading account summary.
    start_balance_sol is read directly from the challenge row — fixed forever.
    """
    start_balance = float(challenge.get("start_balance_sol") or 0)

    if start_balance <= 0:
        ch_id     = challenge["id"]
        if ch_id in _start_balance_cache:
            start_balance = _start_balance_cache[ch_id]
        else:
            plan_name = (challenge.get("challenge_plan") or "starter").lower()
            plan_usd  = PLAN_USD.get(plan_name, 350.0)
            sol_price = await fetch_sol_price()
            start_balance = plan_usd / sol_price if sol_price > 0 else 0.0
            _start_balance_cache[ch_id] = start_balance

    db    = await get_client()
    ch_id = challenge["id"]

    trades_res = await (
        db.table("trades")
        .select("pnl_sol")
        .eq("user_id", user_id)
        .eq("challenge_id", ch_id)
        .eq("side", "sell")
        .execute()
    )
    realized_pnl = sum(float(t.get("pnl_sol") or 0) for t in (trades_res.data or []))

    pos_res = await (
        db.table("positions")
        .select("amount_sol_invested")
        .eq("user_id", user_id)
        .eq("challenge_id", ch_id)
        .eq("status", "open")
        .execute()
    )
    invested_sol = sum(float(p.get("amount_sol_invested") or 0) for p in (pos_res.data or []))

    available_sol = max(0.0, start_balance + realized_pnl - invested_sol)
    total_equity  = start_balance + realized_pnl
    pnl_pct       = (realized_pnl / start_balance * 100) if start_balance else 0.0
    drawdown_pct  = max(0.0, -realized_pnl / start_balance * 100) if start_balance else 0.0
    sol_price     = await fetch_sol_price()
    plan_name     = (challenge.get("challenge_plan") or "starter").lower()
    plan_usd      = PLAN_USD.get(plan_name, 350.0)

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
    return {
        "user_id":                    user_id,
        "quick_buy_1":                TRADING.quick_buy_1,
        "quick_buy_2":                TRADING.quick_buy_2,
        "quick_buy_3":                TRADING.quick_buy_3,
        "quick_sell_1":               TRADING.quick_sell_1,
        "quick_sell_2":               TRADING.quick_sell_2,
        "quick_sell_3":               TRADING.quick_sell_3,
        "default_sl_pct":             None,
        "default_tp_pct":             None,
        "default_trailing_stop_pct":  None,
    }


async def upsert_bot_settings(user_id: str, **updates: Any) -> None:
    db      = await get_client()
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


async def get_closed_positions(user_id: str, limit: int = 50) -> list[dict]:
    db = await get_client()
    res = await (
        db.table("positions")
        .select("opened_at, closed_at, token_symbol, amount_sol_invested")
        .eq("user_id", user_id)
        .eq("status", "closed")
        .order("closed_at", desc=True)
        .limit(limit)
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
    """Used by the background monitor."""
    db = await get_client()
    res = await db.table("positions").select("*").eq("status", "open").execute()
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
    trailing_stop_pct: float | None,
) -> dict:
    db = await get_client()
    row = {
        "user_id":              user_id,
        "challenge_id":         challenge_id,
        "token_address":        token_address,
        "token_symbol":         token_symbol,
        "token_name":           token_name,
        "token_logo_url":       token_logo_url,
        "amount_sol_invested":  amount_sol_invested,
        "entry_price_sol":      entry_price_sol,
        "highest_price_sol":    entry_price_sol,
        "entry_market_cap_usd": entry_market_cap_usd,
        "stop_loss_pct":        stop_loss_pct,
        "take_profit_pct":      take_profit_pct,
        "trailing_stop_pct":    trailing_stop_pct,
        "status":               "open",
    }
    res = await db.table("positions").insert(row).execute()
    return res.data[0]


async def update_position_high(position_id: int, current_price_sol: float) -> None:
    db  = await get_client()
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
    db  = await get_client()
    pos = await get_position(position_id)
    if pos is None:
        raise ValueError(f"Position {position_id} not found")

    entry_price  = float(pos["entry_price_sol"])
    invested_sol = float(pos["amount_sol_invested"])
    frac         = min(sell_pct / 100.0, 1.0)

    simulated_tokens = invested_sol / entry_price if entry_price > 0 else 0
    sold_tokens      = simulated_tokens * frac
    received_sol     = sold_tokens * exit_price_sol
    cost_basis       = invested_sol * frac
    pnl_sol          = received_sol - cost_basis
    pnl_pct          = (pnl_sol / cost_basis * 100) if cost_basis > 0 else 0.0

    is_full_close = sell_pct >= 99.99
    new_status    = "closed" if is_full_close else "open"
    new_invested  = invested_sol * (1 - frac) if not is_full_close else 0.0
    closed_at     = _now() if is_full_close else None

    await (
        db.table("positions")
        .update({
            "status":              new_status,
            "amount_sol_invested": new_invested,
            "closed_at":           closed_at,
            "updated_at":          _now(),
        })
        .eq("id", position_id)
        .execute()
    )

    hold_time_seconds: int | None = None
    try:
        opened_str = pos.get("opened_at")
        if opened_str:
            opened = datetime.fromisoformat(opened_str.replace("Z", "+00:00"))
            hold_time_seconds = int((datetime.now(timezone.utc) - opened).total_seconds())
    except Exception:
        pass

    return {
        "position_id":          position_id,
        "user_id":              pos["user_id"],
        "challenge_id":         pos["challenge_id"],
        "token_symbol":         pos["token_symbol"],
        "token_name":           pos.get("token_name"),
        "token_address":        pos["token_address"],
        "entry_price":          entry_price,
        "exit_price":           exit_price_sol,
        "invested_sol":         cost_basis,
        "received_sol":         received_sol,
        "pnl_sol":              pnl_sol,
        "pnl_pct":              pnl_pct,
        "sell_pct":             sell_pct,
        "hold_time_seconds":    hold_time_seconds,
        "opened_at":            pos.get("opened_at"),
        "stop_loss_pct":        pos.get("stop_loss_pct"),
        "take_profit_pct":      pos.get("take_profit_pct"),
        "trailing_stop_pct":    pos.get("trailing_stop_pct"),
        "entry_market_cap_usd": pos.get("entry_market_cap_usd"),
        "token_logo_url":       pos.get("token_logo_url"),
        "is_full_close":        is_full_close,
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
    side: str,
    amount_sol: float,
    entry_price_sol: float | None,
    exit_price_sol: float | None,
    market_cap_usd: float | None,
    pnl_sol: float | None,
    pnl_pct: float | None,
    sell_pct: float | None,
    trigger: str = "manual",
    exit_market_cap_usd: float | None = None,
    liquidity_usd: float | None = None,
    volume_24h_usd: float | None = None,
    price_impact_pct: float | None = None,
    hold_time_seconds: int | None = None,
    entry_time: str | None = None,
    exit_time: str | None = None,
    stop_loss_pct: float | None = None,
    take_profit_pct: float | None = None,
    trailing_stop_pct: float | None = None,
) -> dict:
    db = await get_client()
    row = {
        "user_id":             user_id,
        "challenge_id":        challenge_id,
        "position_id":         position_id,
        "token_address":       token_address,
        "token_symbol":        token_symbol,
        "token_name":          token_name,
        "side":                side,
        "amount_sol":          amount_sol,
        "entry_price_sol":     entry_price_sol,
        "exit_price_sol":      exit_price_sol,
        "market_cap_usd":      market_cap_usd,
        "pnl_sol":             pnl_sol,
        "pnl_pct":             pnl_pct,
        "sell_pct":            sell_pct,
        "trigger":             trigger,
        "exit_market_cap_usd": exit_market_cap_usd,
        "liquidity_usd":       liquidity_usd,
        "volume_24h_usd":      volume_24h_usd,
        "price_impact_pct":    price_impact_pct,
        "hold_time_seconds":   hold_time_seconds,
        "entry_time":          entry_time,
        "exit_time":           exit_time,
        "stop_loss_pct":       stop_loss_pct,
        "take_profit_pct":     take_profit_pct,
        "trailing_stop_pct":   trailing_stop_pct,
    }
    res = await db.table("trades").insert(row).execute()
    return res.data[0]


async def get_trades(
    user_id: str,
    challenge_id: str | None = None,
    limit: int = 500,
) -> list[dict]:
    db = await get_client()
    q  = db.table("trades").select("*").eq("user_id", user_id)
    if challenge_id:
        q = q.eq("challenge_id", challenge_id)
    res = await q.order("created_at", desc=True).limit(limit).execute()
    return res.data or []


# ---------------------------------------------------------------------------
# Challenge stats — updated after every buy/sell
# ---------------------------------------------------------------------------

async def update_challenge_stats(user_id: str, challenge_id: str) -> None:
    db = await get_client()

    pos_res = await (
        db.table("positions")
        .select("id, amount_sol_invested")
        .eq("user_id", user_id)
        .eq("challenge_id", challenge_id)
        .eq("status", "open")
        .execute()
    )
    open_positions = pos_res.data or []
    open_count     = len(open_positions)
    invested_sol   = sum(float(p.get("amount_sol_invested") or 0) for p in open_positions)

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

    # Use cached start_balance so stats don't drift with SOL price
    start_bal = _start_balance_cache.get(challenge_id, 0.0)
    if start_bal <= 0:
        ch_res = await (
            db.table("challenges")
            .select("start_balance_sol, challenge_plan")
            .eq("id", challenge_id)
            .single()
            .execute()
        )
        ch_data   = ch_res.data or {}
        start_bal = float(ch_data.get("start_balance_sol") or 0)
        if start_bal <= 0:
            plan_name = (ch_data.get("challenge_plan") or "starter").lower()
            sol_price = await fetch_sol_price()
            start_bal = PLAN_USD.get(plan_name, 350.0) / sol_price if sol_price > 0 else 0.0
        _start_balance_cache[challenge_id] = start_bal

    challenge_progress = round(max(0.0, realized_pnl / start_bal * 100) if start_bal else 0.0, 2)
    drawdown           = round(max(0.0, -realized_pnl / start_bal * 100) if start_bal else 0.0, 2)
    buying_power_sol   = max(0.0, start_bal + realized_pnl - invested_sol)

    await (
        db.table("challenges")
        .update({
            "open_positions":     open_count,
            "win_rate":           win_rate,
            "drawdown":           drawdown,
            "challenge_progress": challenge_progress,
            "trading_days":       trading_days,
            "realized_pnl_sol":   realized_pnl,
            "buying_power_sol":   buying_power_sol,
            "updated_at":         _now(),
        })
        .eq("id", challenge_id)
        .execute()
    )
