"""
trading.py — Token lookup, buy/sell execution, and position monitor.

Data sources:
  - Helius API      — primary: token metadata (name, symbol, logo) + mint authority status
  - DexScreener     — always: price, volume, liquidity, market cap
  - pump.fun API    — bonding curve progress % for pre-migration tokens

Rules:
  - No auto-sell. Only manual sells, stop-loss, and trailing stop.
  - Trailing stop is stored on the position at creation time.
  - Every buy is validated against live Supabase challenge data.
  - Every sell records full market context for website analytics.
  - GAS_FEE_SOL is deducted on every buy and sell.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

import httpx

import config
import database as db
from config import GAS_FEE_SOL, PLAN_USD, TRADING

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# DexScreener helpers
# ---------------------------------------------------------------------------

async def search_token(query: str) -> list[dict]:
    """Search for Solana tokens. Filters to supported DEXes for ticker searches."""
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
    Fetch current price data for a Solana token.
    For direct address lookups we accept ANY Solana pair (not just supported
    DEXes) so pump.fun-only tokens are always found.
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

    solana_pairs = [p for p in pairs if p.get("chainId") == "solana"]
    if not solana_pairs:
        return None

    preferred = [p for p in solana_pairs if p.get("dexId") in TRADING.supported_dex_ids]
    pool = preferred if preferred else solana_pairs
    pool.sort(key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0), reverse=True)
    return pool[0]


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
        "market_cap":    float(pair.get("marketCap") or pair.get("fdv") or 0),
        "liquidity_usd": float(liq.get("usd") or 0),
        "volume_24h":    float((pair.get("volume") or {}).get("h24") or 0),
        "change_24h":    float((pair.get("priceChange") or {}).get("h24") or 0),
        "dex_id":        pair.get("dexId", ""),
    }


def _extract_market_data(pair: dict | None) -> dict:
    if not pair:
        return {
            "market_cap_usd":   None,
            "liquidity_usd":    None,
            "volume_24h_usd":   None,
            "price_impact_pct": None,
        }
    liq_usd = float((pair.get("liquidity") or {}).get("usd") or 0)
    return {
        "market_cap_usd":   float(pair.get("marketCap") or pair.get("fdv") or 0) or None,
        "liquidity_usd":    liq_usd or None,
        "volume_24h_usd":   float((pair.get("volume") or {}).get("h24") or 0) or None,
        "price_impact_pct": _estimate_price_impact_pct(liq_usd),
    }


# ---------------------------------------------------------------------------
# Helius metadata (primary source for name/symbol/logo)
# ---------------------------------------------------------------------------

async def get_token_metadata_helius(address: str) -> dict | None:
    if not config.HELIUS_API_KEY:
        return None
    url = f"{TRADING.helius_base}/token-metadata?api-key={config.HELIUS_API_KEY}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as http:
            r = await http.post(url, json={
                "mintAccounts":    [address],
                "includeOffChain": True,
                "disableCache":    False,
            })
            r.raise_for_status()
            data = r.json()
            if data and isinstance(data, list):
                return data[0]
    except Exception as e:
        log.warning("Helius metadata fetch failed for %s: %s", address, e)
    return None


# ---------------------------------------------------------------------------
# Helius DAS — mint authority / freeze authority check
# ---------------------------------------------------------------------------

async def get_mint_authority_status(address: str) -> dict:
    """
    Returns a dict:
      renounced: bool  — True when both mintAuthority and freezeAuthority are null/revoked
      mint_authority:   str | None
      freeze_authority: str | None
    Falls back gracefully if Helius is unavailable or key not set.
    """
    if not config.HELIUS_API_KEY:
        return {"renounced": None, "mint_authority": None, "freeze_authority": None}

    url = f"{TRADING.helius_rpc_base}/?api-key={config.HELIUS_API_KEY}"
    payload = {
        "jsonrpc": "2.0",
        "id":      "mint-auth-check",
        "method":  "getAsset",
        "params":  {"id": address},
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as http:
            r = await http.post(url, json=payload)
            r.raise_for_status()
            result = r.json().get("result") or {}

        token_info = result.get("token_info") or {}
        mint_auth   = token_info.get("mint_authority")
        freeze_auth = token_info.get("freeze_authority")

        # Also check supply.mint_authority in some DAS versions
        supply = result.get("supply") or {}
        if mint_auth is None:
            mint_auth = supply.get("mint_authority")

        renounced = (mint_auth is None or mint_auth == "") and (
            freeze_auth is None or freeze_auth == ""
        )
        return {
            "renounced":        renounced,
            "mint_authority":   mint_auth,
            "freeze_authority": freeze_auth,
        }
    except Exception as e:
        log.warning("Mint authority check failed for %s: %s", address, e)
        return {"renounced": None, "mint_authority": None, "freeze_authority": None}


# ---------------------------------------------------------------------------
# pump.fun bonding curve progress
# ---------------------------------------------------------------------------

async def get_pump_bonding_curve(address: str) -> float | None:
    """
    Returns the bonding curve completion percentage (0–100) for tokens still
    on pump.fun's bonding curve, or None if the token has migrated to a DEX
    or the API is unreachable.
    """
    url = f"{TRADING.pumpfun_api}/coins/{address}"
    try:
        async with httpx.AsyncClient(timeout=8.0) as http:
            r = await http.get(url, headers={"Accept": "application/json"})
            if r.status_code == 404:
                return None
            r.raise_for_status()
            data = r.json()

        # complete = True means the token has graduated off the curve
        if data.get("complete"):
            return None

        progress = data.get("bonding_curve_progress")
        if progress is not None:
            return float(progress)

        # Derive manually from virtual reserves if field missing
        vr  = float(data.get("virtual_sol_reserves") or 0)
        vt  = float(data.get("virtual_token_reserves") or 0)
        if vr > 0 and vt > 0:
            # heuristic: graduated once ~85 SOL in reserves
            return min(vr / 85.0 * 100, 99.9)

        return None
    except Exception as e:
        log.debug("pump.fun bonding curve fetch failed for %s: %s", address, e)
        return None


# ---------------------------------------------------------------------------
# Price impact estimate
# ---------------------------------------------------------------------------

def estimate_price_impact(liquidity_usd: float, trade_sol: float = 1.0, sol_price: float = 150.0) -> str:
    if liquidity_usd <= 0:
        return "—"
    return f"~{(trade_sol * sol_price / liquidity_usd) * 100:.2f}%"


def _estimate_price_impact_pct(liquidity_usd: float, trade_sol: float = 1.0, sol_price: float = 150.0) -> float | None:
    if liquidity_usd <= 0:
        return None
    return round((trade_sol * sol_price / liquidity_usd) * 100, 4)


# ---------------------------------------------------------------------------
# Full token info — Helius + DexScreener + pump.fun + mint-auth in parallel
# ---------------------------------------------------------------------------

async def get_full_token_info(address: str) -> dict | None:
    """
    Fetch complete token info. All four sources fire in parallel.
      - DexScreener  — price / liquidity / volume / market cap (always)
      - Helius meta  — name, symbol, logo (priority over DexScreener)
      - Helius DAS   — mint authority / renounced status
      - pump.fun     — bonding curve %, only for pre-migration tokens

    Returns None only if DexScreener cannot find the token at all.
    """
    dex_pair, helius_meta, mint_status, curve_pct = await asyncio.gather(
        get_token_price(address),
        get_token_metadata_helius(address),
        get_mint_authority_status(address),
        get_pump_bonding_curve(address),
    )

    name = symbol = logo = None

    if helius_meta:
        on_chain  = (helius_meta.get("onChainMetadata") or {}).get("metadata", {}).get("data", {})
        off_chain = helius_meta.get("offChainData") or {}
        name   = (on_chain.get("name")   or off_chain.get("name")   or "").strip() or None
        symbol = (on_chain.get("symbol") or off_chain.get("symbol") or "").strip() or None
        logo   = off_chain.get("image")

    if dex_pair:
        base   = dex_pair.get("baseToken") or {}
        name   = name   or base.get("name",   "Unknown")
        symbol = symbol or base.get("symbol", "???")
        logo   = logo   or (dex_pair.get("info") or {}).get("imageUrl")
    elif not name:
        return None

    # If DexScreener has a pair, the token has migrated — ignore curve_pct
    if dex_pair:
        liq     = dex_pair.get("liquidity") or {}
        changes = dex_pair.get("priceChange") or {}
        vol     = dex_pair.get("volume") or {}
        liq_usd = float(liq.get("usd") or 0)
        sol_price = await db.fetch_sol_price()
        return {
            "address":        address,
            "symbol":         symbol,
            "name":           name,
            "logo_url":       logo,
            "price_usd":      float(dex_pair.get("priceUsd") or 0),
            "market_cap":     float(dex_pair.get("marketCap") or dex_pair.get("fdv") or 0),
            "liquidity_usd":  liq_usd,
            "volume_24h":     float(vol.get("h24") or 0),
            "change_5m":      float(changes.get("m5") or 0),
            "change_1h":      float(changes.get("h1") or 0),
            "change_6h":      float(changes.get("h6") or 0),
            "change_24h":     float(changes.get("h24") or 0),
            "price_impact":   estimate_price_impact(liq_usd, sol_price=sol_price),
            "dex_url":        dex_pair.get("url", ""),
            # Enriched fields
            "renounced":      mint_status.get("renounced"),
            "bonding_curve":  None,   # migrated — no curve data shown
        }
    else:
        # Pre-migration token (pump.fun bonding curve only)
        return {
            "address":        address,
            "symbol":         symbol or "???",
            "name":           name or "Unknown",
            "logo_url":       logo,
            "price_usd":      0.0,
            "market_cap":     0.0,
            "liquidity_usd":  0.0,
            "volume_24h":     0.0,
            "change_5m":      0.0,
            "change_1h":      0.0,
            "change_6h":      0.0,
            "change_24h":     0.0,
            "price_impact":   "—",
            "dex_url":        "",
            "renounced":      mint_status.get("renounced"),
            "bonding_curve":  curve_pct,
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
    trailing_stop_pct: float | None,
    liquidity_usd: float | None = None,
    volume_24h_usd: float | None = None,
    price_impact_pct: float | None = None,
) -> dict:
    challenge_id = challenge["id"]

    if (challenge.get("status") or "").lower() != "active":
        return {"ok": False, "error": f"Your challenge is not active (status: {challenge.get('status')})."}

    open_positions = await db.get_open_positions(user_id)
    if len(open_positions) >= TRADING.max_open_positions:
        return {"ok": False, "error": f"Max {TRADING.max_open_positions} open positions reached."}
    if any(p["token_address"] == token_address for p in open_positions):
        return {"ok": False, "error": "You already have an open position in this token."}

    start_bal = float(challenge.get("start_balance_sol") or 0)
    if start_bal <= 0:
        plan_name = (challenge.get("challenge_plan") or "starter").lower()
        sol_price = await db.fetch_sol_price()
        start_bal = PLAN_USD.get(plan_name, 350.0) / sol_price if sol_price > 0 else 0.0

    max_alloc = start_bal * TRADING.max_allocation_pct / 100.0
    if start_bal > 0 and amount_sol > max_alloc:
        return {
            "ok": False,
            "error": (
                f"Position size exceeds the {TRADING.max_allocation_pct:.0f}% max allocation rule.\n\n"
                f"Max allowed: `{max_alloc:.4f} SOL`\n"
                f"Requested:   `{amount_sol:.4f} SOL`"
            ),
        }

    if float(challenge.get("drawdown") or 0) >= TRADING.max_drawdown_pct:
        return {
            "ok": False,
            "error": (
                f"Trading is locked. Drawdown ({challenge.get('drawdown', 0):.2f}%) "
                f"has reached the {TRADING.max_drawdown_pct:.0f}% maximum."
            ),
        }

    entry_time = datetime.now(timezone.utc).isoformat()

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
        trailing_stop_pct=trailing_stop_pct,
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
        liquidity_usd=liquidity_usd,
        volume_24h_usd=volume_24h_usd,
        price_impact_pct=price_impact_pct,
        entry_time=entry_time,
        stop_loss_pct=stop_loss_pct,
        take_profit_pct=take_profit_pct,
        trailing_stop_pct=trailing_stop_pct,
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
    exit_market_cap_usd: float | None = None,
    liquidity_usd: float | None = None,
    volume_24h_usd: float | None = None,
    price_impact_pct: float | None = None,
) -> dict:
    exit_time = datetime.now(timezone.utc).isoformat()
    result    = await db.close_position(position_id, exit_price_sol, sell_pct)

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
        market_cap_usd=exit_market_cap_usd,
        pnl_sol=result["pnl_sol"],
        pnl_pct=result["pnl_pct"],
        sell_pct=sell_pct,
        trigger=trigger,
        exit_market_cap_usd=exit_market_cap_usd,
        liquidity_usd=liquidity_usd,
        volume_24h_usd=volume_24h_usd,
        price_impact_pct=price_impact_pct,
        hold_time_seconds=result.get("hold_time_seconds"),
        entry_time=result.get("opened_at"),
        exit_time=exit_time,
        stop_loss_pct=_to_float(result.get("stop_loss_pct")),
        take_profit_pct=_to_float(result.get("take_profit_pct")),
        trailing_stop_pct=_to_float(result.get("trailing_stop_pct")),
    )

    await db.update_challenge_stats(result["user_id"], result["challenge_id"])
    return result


def _to_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Background position monitor
# ---------------------------------------------------------------------------

async def check_all_positions(app) -> None:
    """
    Check every open position for SL / TP / trailing-stop triggers.
    Positions are checked in parallel using asyncio.gather.
    """
    positions = await db.get_all_open_positions()
    if not positions:
        return

    user_ids = list({p["user_id"] for p in positions})
    client   = await db.get_client()
    prof_res = await (
        client.table("profiles")
        .select("id, telegram_id, username, telegram_username")
        .in_("id", user_ids)
        .execute()
    )
    profile_map: dict[str, dict] = {p["id"]: p for p in (prof_res.data or [])}
    sol_price = await db.fetch_sol_price()
    if sol_price <= 0:
        return

    await asyncio.gather(*[
        _check_position(pos, sol_price, profile_map, app)
        for pos in positions
    ], return_exceptions=True)


async def _check_position(
    position: dict,
    sol_price: float,
    profile_map: dict[str, dict],
    app,
) -> None:
    try:
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

        pnl_pct = (current_price - entry_price) / entry_price * 100 if entry_price > 0 else 0
        trigger: str | None = None

        if sl_pct is not None and pnl_pct <= -float(sl_pct):
            trigger = "stop_loss"
        elif tp_pct is not None and pnl_pct >= float(tp_pct):
            trigger = "take_profit"
        elif trailing_pct is not None and highest_price > 0:
            drop_pct = (current_price - highest_price) / highest_price * 100
            if drop_pct <= -float(trailing_pct):
                trigger = "trailing_stop"

        if trigger is None:
            return

        market_data = _extract_market_data(pair)
        result = await execute_sell(
            position_id=position["id"],
            exit_price_sol=current_price,
            sell_pct=100.0,
            trigger=trigger,
            **market_data,
        )

        profile = profile_map.get(position["user_id"])
        if not profile:
            return
        tg_id_raw = profile.get("telegram_id")
        if not tg_id_raw:
            return
        try:
            tg_id = int(tg_id_raw)
            pnl   = result["pnl_sol"]
            sign  = "+" if pnl >= 0 else ""
            emoji = "🟢" if pnl >= 0 else "🔴"
            label = trigger.replace("_", " ").title()
            await app.bot.send_message(
                chat_id=tg_id,
                text=(
                    f"{emoji} *{label} Triggered*\n\n"
                    f"Token: `{result['token_symbol']}`\n"
                    f"PnL: `{sign}{pnl:.4f} SOL ({sign}{result['pnl_pct']:.2f}%)`\n"
                    f"Sold: 100% of position"
                ),
                parse_mode="Markdown",
            )
        except Exception as e:
            log.warning("Failed to notify user %s: %s", tg_id_raw, e)

    except Exception as e:
        log.error("Error checking position %s: %s", position.get("id"), e)
