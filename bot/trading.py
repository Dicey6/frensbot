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
# Persistent HTTP client — reused across all requests (avoids per-call
# connection-pool setup overhead which is the #1 latency killer)
# ---------------------------------------------------------------------------

_http_client: httpx.AsyncClient | None = None


def _client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(12.0, connect=4.0),
            limits=httpx.Limits(max_connections=30, max_keepalive_connections=15),
            follow_redirects=True,
        )
    return _http_client


async def _safe(coro, *, timeout: float = 5.0, default=None):
    """Run coro with an individual timeout; return default on any error."""
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except Exception:
        return default


# ---------------------------------------------------------------------------
# DexScreener helpers
# ---------------------------------------------------------------------------

def _best_solana_pair(pairs: list[dict]) -> dict | None:
    """Pick the highest-liquidity Solana pair from a list, preferring known DEXes."""
    solana = [p for p in pairs if p.get("chainId") == "solana"]
    if not solana:
        return None
    preferred = [p for p in solana if p.get("dexId") in TRADING.supported_dex_ids]
    pool = preferred if preferred else solana
    pool.sort(key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0), reverse=True)
    return pool[0]


async def search_token(query: str) -> list[dict]:
    """Search for Solana tokens. Filters to supported DEXes for ticker searches."""
    url = f"{TRADING.dexscreener_base}/search?q={query}"
    try:
        r = await _client().get(url)
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


async def _dex_token_v1(token_address: str) -> dict | None:
    """
    DexScreener legacy API: /latest/dex/tokens/{address}
    Returns {"pairs": [...]} — can miss tokens or return empty on flaky days.
    """
    url = f"{TRADING.dexscreener_base}/tokens/{token_address}"
    try:
        r = await _client().get(url, timeout=8.0)
        r.raise_for_status()
        return _best_solana_pair(r.json().get("pairs") or [])
    except Exception as e:
        log.debug("DexScreener v1 failed for %s: %s", token_address, e)
        return None


async def _dex_token_v2(token_address: str) -> dict | None:
    """
    DexScreener newer API: /tokens/v1/solana/{address}
    Returns a JSON array directly — more reliable for newly listed tokens.
    """
    url = f"https://api.dexscreener.com/tokens/v1/solana/{token_address}"
    try:
        r = await _client().get(url, timeout=8.0)
        r.raise_for_status()
        data = r.json()
        # Response is a list of pairs directly
        if isinstance(data, list):
            return _best_solana_pair(data)
        # Some versions wrap it
        if isinstance(data, dict):
            return _best_solana_pair(data.get("pairs") or [])
        return None
    except Exception as e:
        log.debug("DexScreener v2 failed for %s: %s", token_address, e)
        return None


async def get_token_price(token_address: str) -> dict | None:
    """
    Fetch current price data for a Solana token.
    Runs both DexScreener v1 and v2 in parallel — uses whichever has the
    higher-liquidity pair so that any CA on any Solana DEX is always found.
    """
    v1, v2 = await asyncio.gather(
        _safe(_dex_token_v1(token_address), timeout=9.0),
        _safe(_dex_token_v2(token_address), timeout=9.0),
    )
    # Pick the pair with higher liquidity
    candidates = [p for p in (v1, v2) if p]
    if not candidates:
        return None
    candidates.sort(key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0), reverse=True)
    return candidates[0]


async def get_pair_by_pair_address(pair_address: str) -> dict | None:
    """
    Look up a DexScreener pair by its pool/pair address (not token address).
    Used as a fallback when the pasted address is a liquidity pool address
    (common with migrated pump.fun tokens on DexScreener URLs).
    Returns the best Solana pair, or None if not found.
    """
    url = f"{TRADING.dexscreener_base}/pairs/solana/{pair_address}"
    try:
        r = await _client().get(url)
        r.raise_for_status()
        data = r.json()
        # Response can be {"pair": {...}} or {"pairs": [...]}
        pair = data.get("pair")
        if pair and pair.get("chainId") == "solana":
            return pair
        pairs = data.get("pairs") or []
        solana = [p for p in pairs if p.get("chainId") == "solana"]
        if solana:
            solana.sort(key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0), reverse=True)
            return solana[0]
    except Exception as e:
        log.debug("DexScreener pairs lookup failed for %s: %s", pair_address, e)
    return None


async def _jupiter_price(token_address: str) -> float | None:
    """
    Jupiter price API — last-resort price fallback for tokens with no DEX pair.
    Returns USD price or None.
    """
    url = f"https://api.jup.ag/price/v2?ids={token_address}"
    try:
        r = await _client().get(url, timeout=6.0)
        r.raise_for_status()
        data = r.json().get("data") or {}
        entry = data.get(token_address) or {}
        price = entry.get("price")
        return float(price) if price else None
    except Exception as e:
        log.debug("Jupiter price failed for %s: %s", token_address, e)
        return None


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
# Helius DAS — single getAsset call returns metadata + mint auth + price
# ---------------------------------------------------------------------------

async def get_helius_asset(address: str) -> dict | None:
    """
    One Helius DAS getAsset call that returns everything we need:
      name, symbol, image, mint/freeze authority, and price_per_token.
    This is the PRIMARY token lookup — called before DexScreener.
    """
    if not config.HELIUS_API_KEY:
        return None
    url = f"{TRADING.helius_rpc_base}/?api-key={config.HELIUS_API_KEY}"
    payload = {
        "jsonrpc": "2.0",
        "id":      "get-asset",
        "method":  "getAsset",
        "params":  {
            "id":             address,
            "displayOptions": {"showFungibleExtensions": True},
        },
    }
    try:
        r = await _client().post(url, json=payload)
        r.raise_for_status()
        result = r.json().get("result")
        return result or None
    except Exception as e:
        log.warning("Helius getAsset failed for %s: %s", address, e)
        return None


def _parse_helius_asset(asset: dict) -> dict:
    """Extract the fields we care about from a Helius DAS getAsset result."""
    content    = asset.get("content") or {}
    metadata   = content.get("metadata") or {}
    links      = content.get("links") or {}
    files      = content.get("files") or []
    token_info = asset.get("token_info") or {}
    price_info = token_info.get("price_info") or {}

    name   = (metadata.get("name")   or "").strip() or None
    symbol = (metadata.get("symbol") or "").strip() or None
    logo   = links.get("image") or (files[0].get("uri") if files else None)

    mint_auth   = token_info.get("mint_authority")
    freeze_auth = token_info.get("freeze_authority")
    # Also check legacy supply field in some DAS versions
    if mint_auth is None:
        mint_auth = (asset.get("supply") or {}).get("mint_authority")
    renounced = (
        (mint_auth is None or mint_auth == "")
        and (freeze_auth is None or freeze_auth == "")
    )

    helius_price = float(price_info.get("price_per_token") or 0)

    return {
        "name":        name,
        "symbol":      symbol,
        "logo":        logo,
        "renounced":   renounced,
        "price_usd":   helius_price,
    }


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
        r = await _client().get(url, headers={"Accept": "application/json"})
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
    Fetch complete token info for ANY Solana CA — migrated, pump.fun, or DEX-only.

    Strategy (all fired in parallel):
      1. Helius getAsset          — identity (name/symbol/logo/renounced) + price
      2. DexScreener v1 + v2      — both endpoints fired together inside get_token_price()
      3. pump.fun bonding curve   — for pre-migration tokens

    If DexScreener returns nothing by token address, the pasted address might be a
    pool/pair address. We try /pairs/solana/ and resolve the real token CA.

    If nothing is on any DEX, Jupiter price API is used as a last-resort price source
    so the token page still shows accurate price data.
    """
    # Fire everything in parallel for max speed
    helius_asset, dex_pair, curve_pct = await asyncio.gather(
        _safe(get_helius_asset(address),       timeout=7.0),
        _safe(get_token_price(address),        timeout=10.0),   # already runs v1+v2 in parallel
        _safe(get_pump_bonding_curve(address), timeout=5.0),
    )

    # ── Pool/pair address fallback ───────────────────────────────────────────
    # If both DexScreener endpoints returned nothing, the address might be a
    # pool/pair address rather than a token mint (common with DexScreener URLs).
    resolved_address = address
    if not dex_pair:
        pair_lookup = await _safe(get_pair_by_pair_address(address), timeout=7.0)
        if pair_lookup:
            base       = pair_lookup.get("baseToken") or {}
            token_addr = base.get("address")
            if token_addr and token_addr != address:
                log.info("Resolved pair address %s → token %s", address, token_addr)
                resolved_address = token_addr
                dex_pair         = pair_lookup
                # Re-fetch Helius and bonding curve for the real token address
                if not helius_asset:
                    helius_asset = await _safe(get_helius_asset(token_addr), timeout=7.0)
                if curve_pct is None:
                    curve_pct = await _safe(get_pump_bonding_curve(token_addr), timeout=5.0)
            else:
                dex_pair = pair_lookup

    # ── Parse Helius ─────────────────────────────────────────────────────────
    name = symbol = logo = None
    renounced    = None
    helius_price = 0.0

    if helius_asset:
        parsed       = _parse_helius_asset(helius_asset)
        name         = parsed["name"]
        symbol       = parsed["symbol"]
        logo         = parsed["logo"]
        renounced    = parsed["renounced"]
        helius_price = parsed["price_usd"]

    # ── Enrich from DexScreener ───────────────────────────────────────────────
    if dex_pair:
        base   = dex_pair.get("baseToken") or {}
        name   = name   or base.get("name",   "Unknown")
        symbol = symbol or base.get("symbol", "???")
        logo   = logo   or (dex_pair.get("info") or {}).get("imageUrl")

    # ── Nothing found at all — give up ───────────────────────────────────────
    if not dex_pair and not name:
        log.warning("Token not found on any source: %s", address)
        return None

    # ── Build result ─────────────────────────────────────────────────────────
    sol_price = await db.fetch_sol_price()

    if dex_pair:
        liq     = dex_pair.get("liquidity") or {}
        changes = dex_pair.get("priceChange") or {}
        vol     = dex_pair.get("volume") or {}
        liq_usd = float(liq.get("usd") or 0)
        # Prefer DEX price (real-time), fall back to Helius price
        price_usd = float(dex_pair.get("priceUsd") or 0) or helius_price
        return {
            "address":       resolved_address,
            "symbol":        symbol or "???",
            "name":          name or "Unknown",
            "logo_url":      logo,
            "price_usd":     price_usd,
            "market_cap":    float(dex_pair.get("marketCap") or dex_pair.get("fdv") or 0),
            "liquidity_usd": liq_usd,
            "volume_24h":    float(vol.get("h24") or 0),
            "change_5m":     float(changes.get("m5") or 0),
            "change_1h":     float(changes.get("h1") or 0),
            "change_6h":     float(changes.get("h6") or 0),
            "change_24h":    float(changes.get("h24") or 0),
            "price_impact":  estimate_price_impact(liq_usd, sol_price=sol_price),
            "dex_url":       dex_pair.get("url", ""),
            "renounced":     renounced,
            "bonding_curve": None,   # migrated/listed — no bonding curve shown
        }
    else:
        # Helius found it but no DEX pair — try Jupiter for a live price
        jupiter_price = await _safe(_jupiter_price(resolved_address), timeout=6.0)
        final_price   = jupiter_price or helius_price
        return {
            "address":       resolved_address,
            "symbol":        symbol or "???",
            "name":          name or "Unknown",
            "logo_url":      logo,
            "price_usd":     final_price,
            "market_cap":    0.0,
            "liquidity_usd": 0.0,
            "volume_24h":    0.0,
            "change_5m":     0.0,
            "change_1h":     0.0,
            "change_6h":     0.0,
            "change_24h":    0.0,
            "price_impact":  "—",
            "dex_url":       "",
            "renounced":     renounced,
            "bonding_curve": curve_pct,
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
                f"Max allowed: `{max_alloc:.4f} RH`\n"
                f"Requested:   `{amount_sol:.4f} RH`"
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

    # ── Create position (this is the critical write — must succeed)
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

    # ── Record trade and update stats — log failures but don't propagate them.
    # The position is already created in the DB; raising an exception here would
    # cause the bot to show an error while the position is visible in /positions.
    try:
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
    except Exception as e:
        log.error("record_trade (buy) failed for position %s: %s", position["id"], e)

    try:
        await db.update_challenge_stats(user_id, challenge_id)
    except Exception as e:
        log.error("update_challenge_stats (buy) failed: %s", e)

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

    # ── Close the position (critical write — propagate failures normally)
    result = await db.close_position(position_id, exit_price_sol, sell_pct)

    # ── Record trade and update stats — log failures but don't propagate.
    # The position is already closed; raising here makes the bot show an error
    # even though the sell went through successfully.
    try:
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
    except Exception as e:
        log.error("record_trade (sell) failed for position %s: %s", position_id, e)

    try:
        await db.update_challenge_stats(result["user_id"], result["challenge_id"])
    except Exception as e:
        log.error("update_challenge_stats (sell) failed: %s", e)

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
                    f"PnL: `{sign}{pnl:.4f} RH ({sign}{result['pnl_pct']:.2f}%)`\n"
                    f"Sold: 100% of position"
                ),
                parse_mode="Markdown",
            )
        except Exception as e:
            log.warning("Failed to notify user %s: %s", tg_id_raw, e)

    except Exception as e:
        log.error("Error checking position %s: %s", position.get("id"), e)
