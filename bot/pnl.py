"""
pnl.py — Pillow-based PnL card renderer.

Card size: 1920 × 1080 px (16:9 — optimal for sharing on all platforms).
Uses a programmatically-drawn dark background. If you want to overlay text
onto a custom PNG template instead, set TEMPLATE_PATH below and everything
will composite onto it automatically.

Exports:
  generate_position_card()  — single-position trade card (sent after every sell)
  generate_pnl_card()       — full account summary card (/pnl command)

============================================================
  COORDINATE / SIZE MAP — adjust these without touching logic
============================================================
"""

from __future__ import annotations

import io
import logging
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
#  TEMPLATE (optional)
#  Set to the path of a 1920×1080 PNG to use as background.
#  Leave as None to use the programmatic dark background.
# ============================================================
TEMPLATE_PATH: str | None = None   # e.g. "assets/pnl_template.png"

# ============================================================
#  PADDING & LAYOUT
# ============================================================
PAD          = 72    # outer horizontal padding
HEADER_H     = 90    # height reserved for header bar
FOOTER_H     = 60    # height reserved for footer bar
DIVIDER_Y    = HEADER_H + 10  # y-position of horizontal divider under header

# ============================================================
#  POSITION CARD — text field positions (y-baseline)
# ============================================================
POS_SYMBOL_Y      = 110   # token symbol (large)
POS_NAME_Y        = 186   # token name (small)
POS_ADDRESS_Y     = 216   # shortened address (small muted)
POS_PNL_PCT_Y     = 260   # PnL % hero (very large)
POS_PNL_SOL_Y     = 410   # PnL SOL (medium)

# Stats tile grid
POS_TILE_Y1       = 500   # top of first tile row
POS_TILE_H        = 180   # tile height
POS_TILE_GAP      = 18    # gap between tiles

# ============================================================
#  ACCOUNT CARD — text field positions
# ============================================================
ACC_PNL_PCT_Y     = 110   # realized PnL % hero
ACC_PNL_SOL_Y     = 226   # realized PnL in SOL
ACC_BAR_Y         = 290   # challenge progress bar top
ACC_BAR_H         = 18
ACC_BAR_LABEL_Y   = 316   # label under bar
ACC_TILE_Y1       = 370   # top of first tile row
ACC_TILE_H        = 178
ACC_TILE_GAP      = 18

# ============================================================
#  FONT SIZES
# ============================================================
FONT_BRAND        = 38    # "FUNDED FRENS" in header
FONT_PLAN_BADGE   = 18
FONT_USERNAME     = 22
FONT_SYMBOL       = 64    # token symbol on position card
FONT_NAME         = 26
FONT_ADDRESS      = 18
FONT_PNL_HERO     = 128   # the big PnL % number
FONT_PNL_SOL      = 50
FONT_TILE_VALUE   = 26
FONT_TILE_LABEL   = 16
FONT_FOOTER       = 18
FONT_PROGRESS_LBL = 18

# ============================================================
#  BRAND PALETTE (RGB tuples)
# ============================================================
BG         = (10,  10,  11)
SURFACE    = (22,  22,  26)
BORDER     = (42,  42,  48)
GREEN      = (0,   230, 118)
RED        = (255, 69,  96)
WHITE      = (255, 255, 255)
MUTED      = (107, 107, 128)
ACCENT     = (124, 77,  255)
ACCENT_DIM = (61,  38,  128)


# ---------------------------------------------------------------------------
# Font management
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Drawing helpers
# ---------------------------------------------------------------------------

def _new_card() -> tuple[Image.Image, ImageDraw.ImageDraw]:
    if TEMPLATE_PATH:
        try:
            img = Image.open(TEMPLATE_PATH).convert("RGB").resize((W, H))
            return img, ImageDraw.Draw(img)
        except Exception as e:
            log.warning("Could not load PnL template (%s), using default background", e)
    img  = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)
    return img, draw


def _tw(draw: ImageDraw.ImageDraw, text: str, font) -> int:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0]


def _centered(draw: ImageDraw.ImageDraw, y: int, text: str, font, fill=WHITE, cx: int = W // 2) -> None:
    draw.text((cx - _tw(draw, text, font) // 2, y), text, font=font, fill=fill)


def _draw_tile(draw, x1, y1, x2, y2, label: str, value: str, vcol=WHITE) -> None:
    draw.rounded_rectangle([(x1, y1), (x2, y2)], radius=14, fill=SURFACE, outline=BORDER, width=1)
    draw.rounded_rectangle([(x1, y1), (x1 + 6, y2)], radius=4, fill=ACCENT)
    cx = (x1 + x2) // 2
    my = (y1 + y2) // 2
    fv = _font(FONT_TILE_VALUE, bold=True)
    fl = _font(FONT_TILE_LABEL, bold=False)
    draw.text((cx - _tw(draw, value, fv) // 2, my - 22), value, font=fv, fill=vcol)
    draw.text((cx - _tw(draw, label, fl) // 2, my + 12), label, font=fl, fill=MUTED)


def _draw_header(draw, username: str, plan_name: str) -> None:
    # Accent top bar
    draw.rectangle([(0, 0), (W, 8)], fill=ACCENT)

    fb   = _font(FONT_BRAND, bold=True)
    fs   = _font(FONT_USERNAME, bold=False)
    fp   = _font(FONT_PLAN_BADGE, bold=True)

    # Brand name
    funded_w = _tw(draw, "FUNDED", fb)
    draw.text((PAD, 22), "FUNDED", font=fb, fill=ACCENT)
    draw.text((PAD + funded_w + 8, 22), "FRENS", font=fb, fill=WHITE)

    # Plan badge (top-right)
    plan_label = plan_name.upper()
    pw   = _tw(draw, plan_label, fp)
    bx1  = W - PAD - pw - 28
    bx2  = W - PAD
    draw.rounded_rectangle([(bx1, 26), (bx2, 58)], radius=8, fill=ACCENT_DIM)
    draw.text((bx1 + 14, 30), plan_label, font=fp, fill=ACCENT)

    # Username (below plan badge, right-aligned)
    user = f"@{username}" if username else "Trader"
    uw   = _tw(draw, user, fs)
    draw.text((W - PAD - uw, 64), user, font=fs, fill=MUTED)

    # Divider
    draw.line([(PAD, DIVIDER_Y), (W - PAD, DIVIDER_Y)], fill=BORDER, width=1)


def _draw_footer(draw) -> None:
    fy = H - FOOTER_H
    draw.line([(PAD, fy), (W - PAD, fy)], fill=BORDER, width=1)
    f = _font(FONT_FOOTER, bold=False)
    draw.text((PAD, fy + 14), "fundedfrens.com", font=f, fill=MUTED)
    wm = "FundedFrens Trading Terminal"
    draw.text((W - PAD - _tw(draw, wm, f), fy + 14), wm, font=f, fill=MUTED)


def _fmt_sol(v: float) -> str:
    sign = "+" if v >= 0 else ""
    return f"{sign}{v:.4f} SOL"


def _fmt_pct(v: float) -> str:
    sign = "+" if v >= 0 else ""
    return f"{sign}{v:.2f}%"


def _fmt_usd(v: float) -> str:
    if v <= 0:             return "N/A"
    if v >= 1_000_000_000: return f"${v / 1_000_000_000:.2f}B"
    if v >= 1_000_000:     return f"${v / 1_000_000:.2f}M"
    if v >= 1_000:         return f"${v / 1_000:.1f}K"
    return f"${v:,.0f}"


def _to_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92, optimize=True)
    buf.seek(0)
    return buf.read()


# ---------------------------------------------------------------------------
# Position card  (sent automatically after every sell, and on-demand)
# ---------------------------------------------------------------------------

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
    _draw_header(draw, username, plan_name)

    color = GREEN if pnl_pct >= 0 else RED

    # Token symbol + name + address
    _centered(draw, POS_SYMBOL_Y,  token_symbol, _font(FONT_SYMBOL, bold=True), WHITE)
    _centered(draw, POS_NAME_Y,    token_name,   _font(FONT_NAME, bold=False),  MUTED)
    short = f"{token_address[:6]}...{token_address[-4:]}" if token_address else ""
    _centered(draw, POS_ADDRESS_Y, short, _font(FONT_ADDRESS, bold=False), MUTED)

    # PnL hero
    _centered(draw, POS_PNL_PCT_Y, _fmt_pct(pnl_pct), _font(FONT_PNL_HERO, bold=True), color)
    _centered(draw, POS_PNL_SOL_Y, _fmt_sol(pnl_sol), _font(FONT_PNL_SOL, bold=True),  color)

    # Stats grid  3 × 2
    tile_w   = (W - 2 * PAD - 2 * POS_TILE_GAP) // 3

    tiles = [
        ("Invested",      f"{amount_sol_invested:.4f} SOL",          WHITE),
        ("Current Value", f"{current_value_sol:.4f} SOL",            color),
        ("Hold Time",     hold_time_str,                              WHITE),
        ("Entry MC",      _fmt_usd(entry_market_cap_usd or 0),       MUTED),
        ("Exit MC",       _fmt_usd(current_market_cap_usd or 0),     MUTED),
        ("Liquidity",     _fmt_usd(liquidity_usd or 0),              MUTED),
    ]

    for i, (label, value, vcol) in enumerate(tiles):
        col = i % 3
        row = i // 3
        x1  = PAD + col * (tile_w + POS_TILE_GAP)
        y1  = POS_TILE_Y1 + row * (POS_TILE_H + POS_TILE_GAP)
        _draw_tile(draw, x1, y1, x1 + tile_w, y1 + POS_TILE_H, label, value, vcol)

    _draw_footer(draw)
    return _to_bytes(img)


# ---------------------------------------------------------------------------
# Account summary card  (/pnl command)
# ---------------------------------------------------------------------------

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
    _draw_header(draw, username, plan_name)

    color = GREEN if realized_pnl >= 0 else RED

    # PnL hero
    _centered(draw, ACC_PNL_PCT_Y, _fmt_pct(pnl_pct),    _font(FONT_PNL_HERO, bold=True), color)
    _centered(draw, ACC_PNL_SOL_Y, f"Realized PnL  {_fmt_sol(realized_pnl)}", _font(FONT_PNL_SOL, bold=False), color)

    # Challenge progress bar
    bar_w  = W - 2 * PAD
    fill_w = int(bar_w * min(challenge_progress / 10.0, 1.0))
    draw.rounded_rectangle(
        [(PAD, ACC_BAR_Y), (W - PAD, ACC_BAR_Y + ACC_BAR_H)],
        radius=9, fill=SURFACE, outline=BORDER,
    )
    if fill_w > ACC_BAR_H:
        draw.rounded_rectangle(
            [(PAD, ACC_BAR_Y), (PAD + fill_w, ACC_BAR_Y + ACC_BAR_H)],
            radius=9, fill=ACCENT,
        )
    prog_label = f"Challenge Progress  {challenge_progress:.1f}% / 10%"
    _centered(draw, ACC_BAR_LABEL_Y, prog_label, _font(FONT_PROGRESS_LBL, bold=False), MUTED)

    # Stats grid  3 × 2
    tile_w = (W - 2 * PAD - 2 * ACC_TILE_GAP) // 3

    tiles = [
        ("Start Balance",   f"{start_balance:.4f} SOL",   MUTED),
        ("Current Balance", f"{current_balance:.4f} SOL",  color),
        ("Win Rate",        f"{win_rate:.1f}%",            GREEN if win_rate >= 50 else RED),
        ("Total Trades",    str(total_trades),             WHITE),
        ("Trading Days",    str(trading_days),             WHITE),
        ("Max Drawdown",    f"{drawdown:.2f}%",            RED if drawdown > 5 else WHITE),
    ]

    for i, (label, value, vcol) in enumerate(tiles):
        col = i % 3
        row = i // 3
        x1  = PAD + col * (tile_w + ACC_TILE_GAP)
        y1  = ACC_TILE_Y1 + row * (ACC_TILE_H + ACC_TILE_GAP)
        _draw_tile(draw, x1, y1, x1 + tile_w, y1 + ACC_TILE_H, label, value, vcol)

    _draw_footer(draw)
    return _to_bytes(img)
