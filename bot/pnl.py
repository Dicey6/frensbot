"""
pnl.py — Pillow-based PnL card renderer.

Card size  : 1920 × 1080 px (matches the supplied template)
Template   : assets/pnl_template.png  (dark geometric / FUNDEDFRENS branding)

Because the template already carries the FUNDEDFRENS logo and border art,
the programmatic header bar and footer are skipped when a template is loaded.
Username and plan badge are drawn in the top safe-zone instead.

Exports:
  generate_position_card()  — trade result card (auto-sent after every sell)
  generate_pnl_card()       — full account summary card (/pnl command)

============================================================
  COORDINATE MAP — tweak these without touching any logic
  All values are in pixels on a 1920 × 1080 canvas.
  "Safe zone" = the clear dark centre, avoiding the decorative
  geometry on the left (~0-260 px) and right (~1660-1920 px)
  and the bottom-right logo (~x>1530, y>900).
============================================================
"""

from __future__ import annotations

import io
import logging
import os
from pathlib import Path
from typing import Optional

import httpx
from PIL import Image, ImageDraw, ImageFont

import config

log = logging.getLogger(__name__)

# ============================================================
#  CARD DIMENSIONS
# ============================================================
W, H = 1920, 1080

# ============================================================
#  TEMPLATE
#  Full path to the 1920×1080 background PNG.
#  Set to None to fall back to the programmatic dark background.
# ============================================================
TEMPLATE_PATH: str | None = os.path.join(
    os.path.dirname(__file__), "assets", "pnl_template.png"
)

# ============================================================
#  SAFE CONTENT ZONE
#  Keeps text inside the clear dark centre of the template,
#  clear of the left geometric shapes and the right shards.
# ============================================================
SAFE_LEFT  = 270    # px — left edge of readable area
SAFE_RIGHT = 1655   # px — right edge of readable area
SAFE_TOP   = 72     # px — top edge (below template top art)
SAFE_BOT   = 910    # px — bottom edge (above bottom-right logo)

# Horizontal centre of the safe zone (~962)
SAFE_CX = (SAFE_LEFT + SAFE_RIGHT) // 2

# ============================================================
#  META ROW  (username + plan badge, top of safe zone)
# ============================================================
META_Y         = SAFE_TOP + 10   # baseline of username text
META_BADGE_Y   = SAFE_TOP + 6    # top of plan badge rectangle

# ============================================================
#  POSITION CARD — vertical positions (y baseline / y top)
# ============================================================
POS_SYMBOL_Y      = 135   # token symbol — large
POS_NAME_Y        = 215   # token name — small
POS_ADDRESS_Y     = 248   # shortened address — muted small
POS_PNL_PCT_Y     = 290   # PnL % — hero (very large)
POS_PNL_SOL_Y     = 462   # PnL SOL — medium

# Stats tile grid (3 columns × 2 rows)
POS_TILE_Y1       = 545   # top of first tile row
POS_TILE_H        = 163   # tile height
POS_TILE_GAP      = 18    # horizontal/vertical gap between tiles

# ============================================================
#  ACCOUNT SUMMARY CARD — vertical positions
# ============================================================
ACC_PNL_PCT_Y     = 128   # PnL % hero
ACC_PNL_SOL_Y     = 278   # PnL SOL label
ACC_BAR_Y         = 345   # challenge progress bar — top
ACC_BAR_H         = 20    # bar height
ACC_BAR_LABEL_Y   = 374   # label below bar
ACC_TILE_Y1       = 428   # top of first tile row
ACC_TILE_H        = 163
ACC_TILE_GAP      = 18

# ============================================================
#  FONT SIZES  (px)
# ============================================================
FONT_META_USER   = 24    # username in meta row
FONT_META_BADGE  = 18    # plan badge text
FONT_SYMBOL      = 68    # token symbol on position card
FONT_NAME        = 27    # token name
FONT_ADDRESS     = 19    # shortened address
FONT_PNL_HERO    = 132   # the big PnL % number
FONT_PNL_SOL     = 52    # PnL in SOL
FONT_TILE_VALUE  = 27
FONT_TILE_LABEL  = 17
FONT_PROG_LABEL  = 18

# ============================================================
#  BRAND PALETTE  (RGB)
# ============================================================
GREEN      = (0,   230, 118)
RED        = (255, 69,  96)
WHITE      = (255, 255, 255)
MUTED      = (120, 120, 140)
ACCENT     = (124, 77,  255)
ACCENT_DIM = (50,  30,  110)
SURFACE    = (18,  18,  24, 200)   # RGBA for tile fill (semi-transparent)
BORDER     = (60,  60,  74)
YELLOW     = (200, 230, 30)        # matches template yellow-green accent

# ============================================================
#  FONT MANAGEMENT
# ============================================================

_FONT_DIR  = Path(config.FONTS_DIR)
_FONT_BOLD = _FONT_DIR / "JetBrainsMono-Bold.ttf"
_FONT_REG  = _FONT_DIR / "JetBrainsMono-Regular.ttf"

_FONT_URLS = {
    _FONT_BOLD: (
        "https://github.com/JetBrains/JetBrainsMono/raw/master/"
        "fonts/ttf/JetBrainsMono-Bold.ttf"
    ),
    _FONT_REG: (
        "https://github.com/JetBrains/JetBrainsMono/raw/master/"
        "fonts/ttf/JetBrainsMono-Regular.ttf"
    ),
}

_fonts: dict[tuple, ImageFont.FreeTypeFont] = {}


def _download_fonts() -> None:
    _FONT_DIR.mkdir(parents=True, exist_ok=True)
    for path, url in _FONT_URLS.items():
        if not path.exists():
            try:
                data = httpx.get(url, timeout=30, follow_redirects=True).content
                path.write_bytes(data)
                log.info("Downloaded font: %s", path.name)
            except Exception as e:
                log.warning("Font download failed for %s: %s", path.name, e)


def _font(size: int, bold: bool = True) -> ImageFont.FreeTypeFont:
    key = (size, bold)
    if key not in _fonts:
        try:
            _download_fonts()
            path = _FONT_BOLD if bold else _FONT_REG
            _fonts[key] = ImageFont.truetype(str(path), size)
        except Exception:
            _fonts[key] = ImageFont.load_default()
    return _fonts[key]


# ============================================================
#  DRAWING HELPERS
# ============================================================

def _new_card() -> tuple[Image.Image, ImageDraw.ImageDraw]:
    """Load template or fall back to solid dark background."""
    if TEMPLATE_PATH:
        try:
            img = Image.open(TEMPLATE_PATH).convert("RGBA").resize((W, H))
            # Composite onto black to bake the alpha, then convert to RGB for JPEG output
            bg  = Image.new("RGBA", (W, H), (0, 0, 0, 255))
            bg.alpha_composite(img)
            rgb = bg.convert("RGB")
            return rgb, ImageDraw.Draw(rgb)
        except Exception as e:
            log.warning("Could not load PnL template (%s), using default background", e)
    img  = Image.new("RGB", (W, H), (10, 10, 11))
    return img, ImageDraw.Draw(img)


def _tw(draw: ImageDraw.ImageDraw, text: str, font) -> int:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0]


def _th(draw: ImageDraw.ImageDraw, text: str, font) -> int:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[3] - bbox[1]


def _centered(
    draw: ImageDraw.ImageDraw,
    y: int,
    text: str,
    font,
    fill=WHITE,
    cx: int = SAFE_CX,
) -> None:
    x = cx - _tw(draw, text, font) // 2
    draw.text((x, y), text, font=font, fill=fill)


def _draw_tile(
    draw: ImageDraw.ImageDraw,
    x1: int, y1: int, x2: int, y2: int,
    label: str, value: str, vcol=WHITE,
) -> None:
    # Semi-transparent dark fill using a separate RGBA layer
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    od      = ImageDraw.Draw(overlay)
    od.rounded_rectangle([(x1, y1), (x2, y2)], radius=14, fill=(15, 15, 22, 195))
    od.rounded_rectangle([(x1, y1), (x1 + 6, y2)], radius=4, fill=ACCENT + (255,))

    # Merge overlay into draw's image — we need the PIL image object
    # We pass the image via a closure trick; callers supply the img.
    # This function draws on `draw` directly; the caller handles transparency.

    draw.rounded_rectangle(
        [(x1, y1), (x2, y2)], radius=14,
        fill=(15, 15, 22), outline=BORDER, width=1,
    )
    draw.rounded_rectangle([(x1, y1), (x1 + 6, y2)], radius=4, fill=ACCENT)

    cx = (x1 + x2) // 2
    my = (y1 + y2) // 2
    fv = _font(FONT_TILE_VALUE, bold=True)
    fl = _font(FONT_TILE_LABEL, bold=False)
    vw = _tw(draw, value, fv)
    lw = _tw(draw, label, fl)
    draw.text((cx - vw // 2, my - 22), value, font=fv, fill=vcol)
    draw.text((cx - lw // 2, my + 14), label, font=fl, fill=MUTED)


def _draw_meta(draw: ImageDraw.ImageDraw, username: str, plan_name: str) -> None:
    """Username on the left, plan badge on the right — top of safe zone."""
    fu  = _font(FONT_META_USER, bold=False)
    fp  = _font(FONT_META_BADGE, bold=True)

    user  = f"@{username}" if username else "Trader"
    draw.text((SAFE_LEFT + 4, META_Y), user, font=fu, fill=MUTED)

    plan_label = plan_name.upper()
    pw   = _tw(draw, plan_label, fp)
    bx1  = SAFE_RIGHT - pw - 28
    bx2  = SAFE_RIGHT
    draw.rounded_rectangle([(bx1, META_BADGE_Y), (bx2, META_BADGE_Y + 34)], radius=8, fill=ACCENT_DIM)
    draw.text((bx1 + 14, META_BADGE_Y + 6), plan_label, font=fp, fill=ACCENT)


def _tile_grid(
    draw: ImageDraw.ImageDraw,
    tiles: list[tuple[str, str, tuple]],
    y1_start: int,
    tile_h: int,
    tile_gap: int,
    cols: int = 3,
) -> None:
    """Draw a grid of stat tiles within the safe zone."""
    content_w = SAFE_RIGHT - SAFE_LEFT
    tile_w    = (content_w - (cols - 1) * tile_gap) // cols
    for i, (label, value, vcol) in enumerate(tiles):
        col  = i % cols
        row  = i // cols
        x1   = SAFE_LEFT + col * (tile_w + tile_gap)
        y1   = y1_start  + row * (tile_h  + tile_gap)
        _draw_tile(draw, x1, y1, x1 + tile_w, y1 + tile_h, label, value, vcol)


# ============================================================
#  FORMATTING HELPERS
# ============================================================

def _fmt_sol(v: float) -> str:
    sign = "+" if v >= 0 else ""
    return f"{sign}{v:.4f} SOL"


def _fmt_pct(v: float) -> str:
    sign = "+" if v >= 0 else ""
    return f"{sign}{v:.2f}%"


def _fmt_usd(v: float) -> str:
    if not v or v <= 0:            return "N/A"
    if v >= 1_000_000_000:         return f"${v / 1_000_000_000:.2f}B"
    if v >= 1_000_000:             return f"${v / 1_000_000:.2f}M"
    if v >= 1_000:                 return f"${v / 1_000:.1f}K"
    return f"${v:,.0f}"


def _to_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92, optimize=True)
    buf.seek(0)
    return buf.read()


# ============================================================
#  POSITION CARD
#  Sent automatically after every sell and on 🎴 PnL Card tap.
# ============================================================

def generate_position_card(
    *,
    username: str,
    plan_name: str,
    token_symbol: str,
    token_name: str,
    token_address: str,
    entry_price_usd: float,
    current_price_usd: float,
    amount_sol_invested: float,
    current_value_sol: float,
    pnl_sol: float,
    pnl_pct: float,
    entry_market_cap_usd: Optional[float],
    current_market_cap_usd: Optional[float],
    liquidity_usd: Optional[float],
    hold_time_str: str,
    sol_price: float,
) -> bytes:
    img, draw = _new_card()
    color = GREEN if pnl_pct >= 0 else RED

    _draw_meta(draw, username, plan_name)

    # Token identity
    _centered(draw, POS_SYMBOL_Y,  token_symbol, _font(FONT_SYMBOL, bold=True), WHITE)
    _centered(draw, POS_NAME_Y,    token_name,   _font(FONT_NAME,   bold=False), MUTED)
    short = f"{token_address[:6]}...{token_address[-4:]}" if token_address else ""
    _centered(draw, POS_ADDRESS_Y, short, _font(FONT_ADDRESS, bold=False), MUTED)

    # PnL hero
    _centered(draw, POS_PNL_PCT_Y, _fmt_pct(pnl_pct), _font(FONT_PNL_HERO, bold=True), color)
    _centered(draw, POS_PNL_SOL_Y, _fmt_sol(pnl_sol),  _font(FONT_PNL_SOL,  bold=True), color)

    # Stats grid  3 × 2
    tiles = [
        ("Invested",      f"{amount_sol_invested:.4f} SOL",       WHITE),
        ("Current Value", f"{current_value_sol:.4f} SOL",         color),
        ("Hold Time",     hold_time_str,                           WHITE),
        ("Entry MC",      _fmt_usd(entry_market_cap_usd or 0),    MUTED),
        ("Exit MC",       _fmt_usd(current_market_cap_usd or 0),  MUTED),
        ("Liquidity",     _fmt_usd(liquidity_usd or 0),           MUTED),
    ]
    _tile_grid(draw, tiles, POS_TILE_Y1, POS_TILE_H, POS_TILE_GAP)

    return _to_bytes(img)


# ============================================================
#  ACCOUNT SUMMARY CARD  (/pnl command)
# ============================================================

def generate_pnl_card(
    *,
    username: str,
    plan_name: str,
    realized_pnl: float,
    pnl_pct: float,
    win_rate: float,
    total_trades: int,
    start_balance: float,
    current_balance: float,
    challenge_progress: float,
    drawdown: float,
    trading_days: int,
    sol_price: float,
) -> bytes:
    img, draw = _new_card()
    color = GREEN if realized_pnl >= 0 else RED

    _draw_meta(draw, username, plan_name)

    # PnL hero
    _centered(draw, ACC_PNL_PCT_Y, _fmt_pct(pnl_pct),    _font(FONT_PNL_HERO, bold=True), color)
    _centered(
        draw, ACC_PNL_SOL_Y,
        f"Realized PnL  {_fmt_sol(realized_pnl)}",
        _font(FONT_PNL_SOL, bold=False), color,
    )

    # Challenge progress bar
    bar_w  = SAFE_RIGHT - SAFE_LEFT
    fill_w = int(bar_w * min(challenge_progress / 10.0, 1.0))
    draw.rounded_rectangle(
        [(SAFE_LEFT, ACC_BAR_Y), (SAFE_RIGHT, ACC_BAR_Y + ACC_BAR_H)],
        radius=10, fill=(15, 15, 22), outline=BORDER,
    )
    if fill_w > ACC_BAR_H:
        draw.rounded_rectangle(
            [(SAFE_LEFT, ACC_BAR_Y), (SAFE_LEFT + fill_w, ACC_BAR_Y + ACC_BAR_H)],
            radius=10, fill=ACCENT,
        )
    prog_label = f"Challenge Progress  {challenge_progress:.1f}% / 10%"
    _centered(draw, ACC_BAR_LABEL_Y, prog_label, _font(FONT_PROG_LABEL, bold=False), MUTED)

    # Stats grid  3 × 2
    tiles = [
        ("Start Balance",   f"{start_balance:.4f} SOL",      MUTED),
        ("Current Balance", f"{current_balance:.4f} SOL",    color),
        ("Win Rate",        f"{win_rate:.1f}%",               GREEN if win_rate >= 50 else RED),
        ("Total Trades",    str(total_trades),                WHITE),
        ("Trading Days",    str(trading_days),                WHITE),
        ("Max Drawdown",    f"{drawdown:.2f}%",               RED if drawdown > 5 else WHITE),
    ]
    _tile_grid(draw, tiles, ACC_TILE_Y1, ACC_TILE_H, ACC_TILE_GAP)

    return _to_bytes(img)
