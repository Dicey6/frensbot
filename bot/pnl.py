"""
pnl.py — Pillow-based PnL card renderer.

Card size: 1200 × 675 px (16:9, optimal for Twitter/X and Telegram).
Replaces matplotlib for faster rendering and lower memory footprint.

Exports:
  generate_position_card()  — single-position trade card (sent after every sell)
  generate_pnl_card()       — full account summary card (/pnl command)
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

W, H = 1200, 675
PAD  = 48

# Brand palette (RGB tuples)
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
    img  = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)
    return img, draw


def _tw(draw: ImageDraw.ImageDraw, text: str, font) -> int:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0]


def _centered(draw: ImageDraw.ImageDraw, y: int, text: str, font, fill=WHITE, cx: int = W // 2) -> None:
    draw.text((cx - _tw(draw, text, font) // 2, y), text, font=font, fill=fill)


def _draw_tile(draw, x1, y1, x2, y2, label: str, value: str, vcol=WHITE) -> None:
    draw.rounded_rectangle([(x1, y1), (x2, y2)], radius=10, fill=SURFACE, outline=BORDER, width=1)
    draw.rounded_rectangle([(x1, y1), (x1 + 4, y2)], radius=3, fill=ACCENT)
    cx = (x1 + x2) // 2
    my = (y1 + y2) // 2
    fv = _font(18, bold=True)
    fl = _font(12, bold=False)
    draw.text((cx - _tw(draw, value, fv) // 2, my - 16), value, font=fv, fill=vcol)
    draw.text((cx - _tw(draw, label, fl) // 2, my + 8),  label, font=fl, fill=MUTED)


def _draw_header(draw, username: str, plan_name: str) -> None:
    draw.rectangle([(0, 0), (W, 6)], fill=ACCENT)
    fb = _font(28, bold=True)
    fs = _font(16, bold=False)
    draw.text((PAD, 20), "FUNDED", font=fb, fill=ACCENT)
    draw.text((PAD + _tw(draw, "FUNDED", fb) + 6, 20), "FRENS", font=fb, fill=WHITE)
    plan_label = plan_name.upper()
    fp = _font(13, bold=True)
    pw = _tw(draw, plan_label, fp)
    bx1 = W - PAD - pw - 20
    draw.rounded_rectangle([(bx1, 22), (W - PAD, 42)], radius=6, fill=ACCENT_DIM)
    draw.text((bx1 + 10, 25), plan_label, font=fp, fill=ACCENT)
    user = f"@{username}" if username else "Trader"
    uw   = _tw(draw, user, fs)
    draw.text((W - PAD - uw, 48), user, font=fs, fill=MUTED)
    draw.line([(PAD, 74), (W - PAD, 74)], fill=BORDER, width=1)


def _draw_footer(draw) -> None:
    draw.line([(PAD, H - 52), (W - PAD, H - 52)], fill=BORDER, width=1)
    f = _font(14, bold=False)
    draw.text((PAD, H - 36), "fundedfrens.com", font=f, fill=MUTED)
    wm = "FundedFrens Trading Terminal"
    draw.text((W - PAD - _tw(draw, wm, f), H - 36), wm, font=f, fill=MUTED)


def _fmt_sol(v: float) -> str:
    sign = "+" if v >= 0 else ""
    return f"{sign}{v:.4f} SOL"


def _fmt_pct(v: float) -> str:
    sign = "+" if v >= 0 else ""
    return f"{sign}{v:.2f}%"


def _fmt_usd(v: float) -> str:
    if v <= 0:         return "N/A"
    if v >= 1_000_000_000: return f"${v / 1_000_000_000:.2f}B"
    if v >= 1_000_000: return f"${v / 1_000_000:.2f}M"
    if v >= 1_000:     return f"${v / 1_000:.1f}K"
    return f"${v:,.0f}"


def _to_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92, optimize=True)
    buf.seek(0)
    return buf.read()


# ---------------------------------------------------------------------------
# Position card
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

    # Token name + symbol
    _centered(draw, 90,  token_symbol, _font(48, bold=True), WHITE)
    _centered(draw, 150, token_name,   _font(20, bold=False), MUTED)
    short = f"{token_address[:6]}...{token_address[-4:]}" if token_address else ""
    _centered(draw, 176, short, _font(14, bold=False), MUTED)

    # PnL hero
    _centered(draw, 210, _fmt_pct(pnl_pct), _font(88, bold=True), color)
    _centered(draw, 304, _fmt_sol(pnl_sol), _font(36, bold=True), color)

    # Stats grid  3 × 2
    tile_y1 = 370
    tile_h  = 118
    tile_gap = 12
    tile_w   = (W - 2 * PAD - 2 * tile_gap) // 3

    tiles = [
        ("Invested",     f"{amount_sol_invested:.4f} SOL",           WHITE),
        ("Current Value",f"{current_value_sol:.4f} SOL",             color),
        ("Hold Time",    hold_time_str,                               WHITE),
        ("Entry MC",     _fmt_usd(entry_market_cap_usd or 0),        MUTED),
        ("Exit MC",      _fmt_usd(current_market_cap_usd or 0),      MUTED),
        ("Liquidity",    _fmt_usd(liquidity_usd or 0),               MUTED),
    ]

    for i, (label, value, vcol) in enumerate(tiles):
        col = i % 3
        row = i // 3
        x1  = PAD + col * (tile_w + tile_gap)
        y1  = tile_y1 + row * (tile_h + tile_gap)
        _draw_tile(draw, x1, y1, x1 + tile_w, y1 + tile_h, label, value, vcol)

    _draw_footer(draw)
    return _to_bytes(img)


# ---------------------------------------------------------------------------
# Account summary card
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

    # Hero
    _centered(draw, 95,  _fmt_pct(pnl_pct),    _font(80, bold=True), color)
    _centered(draw, 188, f"Realized PnL  {_fmt_sol(realized_pnl)}", _font(28, bold=False), color)

    # Progress bar
    bar_y  = 232
    bar_h  = 14
    bar_w  = W - 2 * PAD
    fill_w = int(bar_w * min(challenge_progress / 10.0, 1.0))
    draw.rounded_rectangle([(PAD, bar_y), (W - PAD, bar_y + bar_h)], radius=7, fill=SURFACE, outline=BORDER)
    if fill_w > 14:
        draw.rounded_rectangle([(PAD, bar_y), (PAD + fill_w, bar_y + bar_h)], radius=7, fill=ACCENT)
    prog_label = f"Challenge Progress  {challenge_progress:.1f}% / 10%"
    fl = _font(14, bold=False)
    _centered(draw, bar_y + 20, prog_label, fl, MUTED)

    # Stats grid  3 × 2
    tile_y1 = bar_y + 52
    tile_h  = 116
    tile_gap = 12
    tile_w   = (W - 2 * PAD - 2 * tile_gap) // 3

    tiles = [
        ("Start Balance",  f"{start_balance:.4f} SOL",    MUTED),
        ("Current Balance",f"{current_balance:.4f} SOL",  color),
        ("Win Rate",       f"{win_rate:.1f}%",             GREEN if win_rate >= 50 else RED),
        ("Total Trades",   str(total_trades),              WHITE),
        ("Trading Days",   str(trading_days),              WHITE),
        ("Max Drawdown",   f"{drawdown:.2f}%",             RED if drawdown > 5 else WHITE),
    ]

    for i, (label, value, vcol) in enumerate(tiles):
        col = i % 3
        row = i // 3
        x1  = PAD + col * (tile_w + tile_gap)
        y1  = tile_y1 + row * (tile_h + tile_gap)
        _draw_tile(draw, x1, y1, x1 + tile_w, y1 + tile_h, label, value, vcol)

    _draw_footer(draw)
    return _to_bytes(img)
