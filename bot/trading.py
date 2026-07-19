"""
trading.py — Token lookup, buy/sell execution, and position monitor.

Integrates with:
  - DexScreener API for live token price data
  - database.py for all persistence
  - config.py for trading limits and constants

Field name corrections from the original bot:
  - profile["auth_user_id"] → profile["id"]
  - positions.user_id is profiles.id (UUID) — all DB queries use this
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

import config
import database as db
from config import TRADING

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# DexScreener helpers
# ---------------------------------------------------------------------------

async def search_token(query: str) -> list[dict]:
    """
    Search DexScreener for Solana tokens matching `query`.
    Returns up to 5 pairs sorted by liquidity (desc).
    """
    url = f"{TRADING.dexscreener_base}/search?q={query}"
    try:
        async with httpx.AsyncClient(timeout=15.0) as http:
            r = await http.get(url)
            r.raise_for_status()
            pairs = r.json().get("pairs") or []
    except Exception as e:
        log.error("DexScreener search error: %s", e)
        return []

    solana_pairs = [
        p for p in pairs
        if p.get("chainId") == "solana"
        and p.get("dexId") in TRADING.supported_dex_ids
        and float((p.get("liquidity") or {}).get("usd") or 0) >= TRADING.min_liquidity_usd
    ]
    solana_pairs.sort(
        key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0),
        reverse=True,
    )
    return solana_pairs[:5]


async def get_token_price(token_address: str) -> dict | None:
    """
    Fetch current price data for a specific Solana token address.
    Returns the highest-liquidity pair or None.
    """
    url = f"{TRADING.dexscreener_base}/tokens/{token_address}"
    try:
        async with httpx.AsyncClient(timeout=15.0) as http:
            r = await http.get(url)
            r.raise_for_status()
            pairs = r.json().get("pairs") or []
    except Exception as e:
        log.error("DexScreener price fetch error for %s: %s", token_address, e)
        return None

    solana_pairs = [
        p for p in pairs
        if p.get("chainId") == "solana"
        and p.get("dexId") in TRADING.supported_dex_ids
    ]
    if not solana_pairs:
        return None

    solana_pairs.sort(
        key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0),
        reverse=True,
    )
    return solana_pairs[0]


def price_in_sol(pair: dict, sol_price_usd: float) -> float:
    """Convert a DexScreener pair's priceUsd to SOL-denominated price."""
    price_usd_raw = pair.get("priceUsd") or pair.get("priceNative")
    if not price_usd_raw or sol_price_usd <= 0:
        return 0.0
    try:
        return float(price_usd_raw) / sol_price_usd
    except (ValueError, TypeError):
        return 0.0


def extract_token_info(pair: dict) -> dict[str, Any]:
    """Extract a clean token info dict from a DexScreener pair."""
    base = pair.get("baseToken") or {}
    liq  = pair.get("liquidity") or {}
    return {
        "address":       base.get("address", ""),
        "symbol":        base.get("symbol", "???"),
        "name":          base.get("name", "Unknown"),
        "logo_url":      (pair.get("info") or {}).get("imageUrl"),
        "price_usd":     float(pair.get("priceUsd") or 0),
        "price_native":  float(pair.get("priceNative") or 0),
        "market_cap":    float(pair.get("marketCap") or pair.get("fdv") or 0),
        "liquidity_usd": float(liq.get("usd") or 0),
        "volume_24h":    float((pair.get("volume") or {}).get("h24") or 0),
        "change_24h":    float((pair.get("priceChange") or {}).get("h24") or 0),
        "dex_id":        pair.get("dexId", ""),
        "pair_address":  pair.get("pairAddress", ""),
    }


# ---------------------------------------------------------------------------
# Buy
# ---------------------------------------------------------------------------

async def execute_buy(
    *,
    user_id: str,
    challenge: dict,
    token_address: str,
    token_symbol: str,
    token_name: str | None,
    token_logo_url: str | None,
    amount_sol: float,
    entry_price_sol: float,
    entry_market_cap_usd: float | None,
    stop_loss_pct: float | None,
    take_profit_pct: float | None,
    auto_sell_pct: float | None,
) -> dict:
    """
    Simulate a buy. Returns {"ok": True, ...} or {"ok": False, "error": ...}.
    Updates challenge stats (Q3) after every successful buy.
    """
    challenge_id   = challenge["id"]
    open_positions = await db.get_open_positions(user_id)

    if len(open_positions) >= TRADING.max_open_positions:
        return {"ok": False, "error": f"Max {TRADING.max_open_positions} open positions reached."}

    if any(p["token_address"] == token_address for p in open_positions):
        return {"ok": False, "error": "You already have an open position in this token."}

    position = await db.create_position(
        user_id=user_id,
        challenge_id=challenge_id,
        token_address=token_address,
        token_symbol=token_symbol,
        token_name=token_name,
        token_logo_url=token_logo_url,
        amount_sol_invested=amount_sol,
        entry_price_sol=entry_price_sol,
        entry_market_cap_usd=entry_market_cap_usd,
        stop_loss_pct=stop_loss_pct,
        take_profit_pct=take_profit_pct,
        auto_sell_pct=auto_sell_pct,
    )

    await db.record_trade(
        user_id=user_id,
        challenge_id=challenge_id,
        position_id=position["id"],
        token_address=token_address,
        token_symbol=token_symbol,
        token_name=token_name,
        side="buy",
        amount_sol=amount_sol,
        entry_price_sol=entry_price_sol,
        exit_price_sol=None,
        market_cap_usd=entry_market_cap_usd,
        pnl_sol=None,
        pnl_pct=None,
        sell_pct=None,
        trigger="manual",
    )

    await db.update_challenge_stats(user_id, challenge_id)

    return {
        "ok":           True,
        "position_id":  position["id"],
        "token_symbol": token_symbol,
        "amount_sol":   amount_sol,
        "entry_price":  entry_price_sol,
    }


# ---------------------------------------------------------------------------
# Sell
# ---------------------------------------------------------------------------

async def execute_sell(
    *,
    position_id: int,
    exit_price_sol: float,
    sell_pct: float = 100.0,
    trigger: str = "manual",
) -> dict:
    """
    Simulate a sell. Records the trade and updates challenge stats (Q3).
    Returns the close result dict (pnl_sol, pnl_pct, etc.).
    """
    result = await db.close_position(position_id, exit_price_sol, sell_pct)

    await db.record_trade(
        user_id=result["user_id"],
        challenge_id=result["challenge_id"],
        position_id=position_id,
        token_address=result["token_address"],
        token_symbol=result["token_symbol"],
        token_name=result.get("token_name"),
        side="sell",
        amount_sol=result["received_sol"],
        entry_price_sol=result["entry_price"],
        exit_price_sol=result["exit_price"],
        market_cap_usd=None,
        pnl_sol=result["pnl_sol"],
        pnl_pct=result["pnl_pct"],
        sell_pct=sell_pct,
        trigger=trigger,
    )

    await db.update_challenge_stats(result["user_id"], result["challenge_id"])
    return result


# ---------------------------------------------------------------------------
# Background position monitor
# ---------------------------------------------------------------------------

async def check_all_positions(app) -> None:
    """
    Check every open position against SL / TP / trailing / auto-sell thresholds.
    Called by the PTB job_queue every TRADING.monitor_interval_seconds seconds.
    Using job_queue (not asyncio.create_task) ensures proper lifecycle management.
    """
    positions = await db.get_all_open_positions()
    if not positions:
        return

    user_ids = list({p["user_id"] for p in positions})
    client   = await db.get_client()
    prof_res = await (
        client.table("profiles")
        .select("id, telegram_id, telegram_username")
        .in_("id", user_ids)
        .execute()
    )
    profile_map: dict[str, dict] = {
        p["id"]: p for p in (prof_res.data or [])
    }

    from database import _fetch_sol_price
    sol_price = await _fetch_sol_price()
    if sol_price <= 0:
        return

    for position in positions:
        try:
            await _check_position(position, sol_price, profile_map, app)
        except Exception as e:
            log.error("Error checking position %s: %s", position.get("id"), e)


async def _check_position(
    position: dict,
    sol_price: float,
    profile_map: dict[str, dict],
    app,
) -> None:
    pair = await get_token_price(position["token_address"])
    if pair is None:
        return

    current_price = price_in_sol(pair, sol_price)
    if current_price <= 0:
        return

    await db.update_position_high(position["id"], current_price)

    entry_price   = float(position["entry_price_sol"])
    highest_price = float(position.get("highest_price_sol") or entry_price)
    sl_pct        = position.get("stop_loss_pct")
    tp_pct        = position.get("take_profit_pct")
    trailing_pct  = position.get("trailing_stop_pct")
    auto_sell_pct = position.get("auto_sell_pct")

    pnl_pct  = (current_price - entry_price) / entry_price * 100 if entry_price > 0 else 0
    trigger  : str | None = None
    sell_pct : float      = 100.0

    if sl_pct and pnl_pct <= -float(sl_pct):
        trigger = "stop_loss"
    elif tp_pct and pnl_pct >= float(tp_pct):
        trigger = "take_profit"
    elif trailing_pct and highest_price > 0:
        if (current_price - highest_price) / highest_price * 100 <= -float(trailing_pct):
            trigger = "trailing_stop"
    elif auto_sell_pct and pnl_pct >= float(auto_sell_pct):
        trigger  = "auto_sell"
        sell_pct = float(auto_sell_pct)

    if trigger is None:
        return

    result = await execute_sell(
        position_id=position["id"],
        exit_price_sol=current_price,
        sell_pct=sell_pct,
        trigger=trigger,
    )

    profile = profile_map.get(position["user_id"])
    if profile:
        tg_id_raw = profile.get("telegram_id")
        if tg_id_raw:
            try:
                tg_id   = int(tg_id_raw)
                pnl     = result["pnl_sol"]
                sign    = "+" if pnl >= 0 else ""
                emoji   = "🟢" if pnl >= 0 else "🔴"
                label   = trigger.replace("_", " ").title()
                await app.bot.send_message(
                    chat_id=tg_id,
                    text=(
                        f"{emoji} *{label} Triggered*\n\n"
                        f"Token: `{result['token_symbol']}`\n"
                        f"PnL: `{sign}{pnl:.4f} SOL ({sign}{result['pnl_pct']:.2f}%)`\n"
                        f"Sold: `{sell_pct:.0f}%` of position"
                    ),
                    parse_mode="Markdown",
                )
            except Exception as e:
                log.warning("Failed to notify user %s: %s", tg_id_raw, e)
