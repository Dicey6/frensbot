"""
config.py — Environment configuration and constants for the HoodFund Telegram bot.
All os.environ access is centralised here.
"""

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


# ---------------------------------------------------------------------------
# Environment helpers
# ---------------------------------------------------------------------------

def _require(key: str) -> str:
    val = os.getenv(key)
    if not val:
        raise RuntimeError(f"Required environment variable '{key}' is not set")
    return val


# ---------------------------------------------------------------------------
# Core credentials
# ---------------------------------------------------------------------------

BOT_TOKEN: str            = _require("TELEGRAM_BOT_TOKEN")
SUPABASE_URL: str         = _require("SUPABASE_URL")
SUPABASE_SERVICE_KEY: str = _require("SUPABASE_SERVICE_ROLE_KEY")

APP_URL: str   = os.getenv("APP_URL", "https://hoodfund.online")
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

# Helius is the primary token metadata source; DexScreener is the fallback
HELIUS_API_KEY: str | None = os.getenv("HELIUS_API_KEY")


# ---------------------------------------------------------------------------
# Challenge plans — fallback values only.
# ---------------------------------------------------------------------------

PLAN_USD: dict[str, float] = {
    "starter":       350.0,
    "advanced":     1100.0,
    "professional": 3500.0,
}


# ---------------------------------------------------------------------------
# Gas fee — fixed SOL amount deducted from every buy and sell
# ---------------------------------------------------------------------------

GAS_FEE_SOL: float = 0.0001


# ---------------------------------------------------------------------------
# Trading constants
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TradingConfig:
    # Position limits
    max_open_positions: int   = 3
    max_allocation_pct: float = 30.0      # max % of demo balance per position

    # Challenge rule limits
    max_drawdown_pct: float = 10.0

    # Default quick-buy amounts (user can override in Settings)
    quick_buy_1: float = 0.1
    quick_buy_2: float = 0.5
    quick_buy_3: float = 1.0

    # Default quick-sell percentages (user can override in Settings)
    quick_sell_1: float = 25.0
    quick_sell_2: float = 50.0
    quick_sell_3: float = 100.0

    # Background monitor
    monitor_interval_seconds: int = 30

    # Supported Solana DEXes (DexScreener dexId values).
    supported_dex_ids: frozenset = field(default_factory=lambda: frozenset({
        "raydium", "orca", "meteora", "pump", "moonshot",
        "fluxbeam", "lifinity", "whirlpool",
    }))

    # Minimum liquidity for ticker search results
    min_liquidity_usd: float = 1_000.0

    # SOL price cache TTL in seconds
    sol_price_cache_ttl: int = 60

    # API endpoints
    dexscreener_base: str = "https://api.dexscreener.com/latest/dex"
    helius_base:      str = "https://api.helius.xyz/v0"
    helius_rpc_base:  str = "https://mainnet.helius-rpc.com"
    pumpfun_api:      str = "https://frontend-api.pump.fun"


TRADING = TradingConfig()


# ---------------------------------------------------------------------------
# PnL card rendering — 1920 × 1080 (16:9)
# ---------------------------------------------------------------------------

CARD_WIDTH  = 1920
CARD_HEIGHT = 1080

ASSETS_DIR = os.path.join(os.path.dirname(__file__), "assets")
FONTS_DIR  = os.path.join(ASSETS_DIR, "fonts")

# Brand colours
COLOUR_BG         = "#0A0A0B"
COLOUR_BG2        = "#111114"
COLOUR_SURFACE    = "#16161A"
COLOUR_BORDER     = "#2A2A30"
COLOUR_GREEN      = "#00E676"
COLOUR_RED        = "#FF4560"
COLOUR_TEXT       = "#FFFFFF"
COLOUR_MUTED      = "#6B6B80"
COLOUR_ACCENT     = "#7C4DFF"
COLOUR_ACCENT_DIM = "#3D2680"
COLOUR_GOLD       = "#FFD700"
