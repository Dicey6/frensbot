"""
config.py — Environment configuration and constants for the FundedFrens Telegram bot.
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

BOT_TOKEN: str          = _require("TELEGRAM_BOT_TOKEN")
SUPABASE_URL: str       = _require("SUPABASE_URL")
SUPABASE_SERVICE_KEY: str = _require("SUPABASE_SERVICE_ROLE_KEY")

APP_URL: str   = os.getenv("APP_URL", "https://fundedfrens.com")
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")


# ---------------------------------------------------------------------------
# Challenge plans — hardcoded to mirror the website's CHALLENGE_PLANS.
# These are the funded (demo) account sizes in USD.
# Do NOT store in the database; derive SOL equivalent at runtime using
# the live SOL/USD price.
# ---------------------------------------------------------------------------

PLAN_USD: dict[str, float] = {
    "starter":       350.0,
    "advanced":     1100.0,
    "professional": 3500.0,
}


# ---------------------------------------------------------------------------
# Trading simulation constants
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TradingConfig:
    # Position limits
    max_open_positions: int   = 3
    max_allocation_pct: float = 30.0      # max % of demo balance per position

    # Default risk parameters (user can override in bot_settings)
    default_buy_sol: float        = 0.1
    default_sl_pct: float         = 20.0
    default_tp_pct: float         = 50.0
    default_auto_sell_pct: float | None = None

    # Background monitor
    monitor_interval_seconds: int = 30

    # Supported Solana DEXes / launchpads (DexScreener dexId values)
    supported_dex_ids: frozenset = field(default_factory=lambda: frozenset({
        "raydium", "orca", "meteora", "pump", "moonshot",
        "fluxbeam", "lifinity", "whirlpool",
    }))

    # Minimum liquidity threshold
    min_liquidity_usd: float = 1_000.0

    # DexScreener base URL
    dexscreener_base: str = "https://api.dexscreener.com/latest/dex"


TRADING = TradingConfig()


# ---------------------------------------------------------------------------
# PnL card rendering
# ---------------------------------------------------------------------------

CARD_WIDTH  = 1920
CARD_HEIGHT = 1080

ASSETS_DIR = os.path.join(os.path.dirname(__file__), "assets")
FONTS_DIR  = os.path.join(ASSETS_DIR, "fonts")

# These string constants are resolved to actual file paths inside pnl.py
FONT_BOLD     = "JBMONO_BOLD"
FONT_SEMIBOLD = "JBMONO_REGULAR"
FONT_REGULAR  = "JBMONO_REGULAR"

LOGO_PATH = os.path.join(ASSETS_DIR, "logo.png")

# Brand colours (dark premium theme)
COLOUR_BG         = "#0A0A0B"
COLOUR_BG2        = "#111114"
COLOUR_SURFACE    = "#18181C"
COLOUR_BORDER     = "#2A2A30"
COLOUR_GREEN      = "#00E676"
COLOUR_GREEN_DIM  = "#00C853"
COLOUR_RED        = "#FF1744"
COLOUR_RED_DIM    = "#D50000"
COLOUR_TEXT       = "#FFFFFF"
COLOUR_MUTED      = "#8888A0"
COLOUR_ACCENT     = "#7C4DFF"
COLOUR_ACCENT_DIM = "#4A148C"
