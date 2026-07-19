"""
pnl.py — Pillow-based PnL card generator (1920×1080).

Generates a professional trading summary card as a PNG bytes buffer.
Called from main.py's /pnl command.

Font: JetBrains Mono downloaded at first use and cached in assets/fonts/.
Logo: uses assets/logo.png if present, otherwise omits it gracefully.
"""

from __future__ import annotations

import io
import logging
import os
import urllib.request
from datetime import datetime, timezone

from PIL import Image, ImageDraw, ImageFont

import config

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Font management
# ---------------------------------------------------------------------------

_FONT_URLS = {
    "JBMONO_BOLD":    "https://github.com/JetBrains/JetBrainsMono/raw/master/fonts/ttf/JetBrainsMono-Bold.ttf",
    "JBMONO_REGULAR": "https://github.com/JetBrains/JetBrainsMono/raw/master/fonts/ttf/JetBrainsMono-Regular.ttf",
}

_FONT_CACHE: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}


def _get_font(alias: str, size: int) -> ImageFont.FreeTypeFont:
    key = (alias, size)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]

    os.makedirs(config.FONTS_DIR, exist_ok=True)
    font_path = os.path.join(config.FONTS_DIR, f"{alias}.ttf")

    if not os.path.exists(font_path):
        url = _FONT_URLS.get(alias)
        if url:
            try:
                urllib.request.urlretrieve(url, font_path)
            except Exception as e:
                log.warning("Font download failed for %s: %s — using default", alias, e)
                font_path = None
        else:
            font_path = None

    try:
        font = ImageFont.truetype(font_path, size) if font_path else ImageFont.load_default()
    except Exception:
        font = ImageFont.load_default()

    _FONT_CACHE[key] = font
    return font


# ---------------------------------------------------------------------------
# Colour helpers
# ---------------------------------------------------------------------------

def _hex(hex_str: str) -> tuple[int, int, int]:
    h = hex_str.lstrip("#")
    return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def _rgba(hex_str: str, alpha: int = 255) -> tuple[int, int, int, int]:
    r, g, b = _hex(hex_str)
    return (r, g, b, alpha)


# ---------------------------------------------------------------------------
# Drawing helpers
# ---------------------------------------------------------------------------

def _text_center(draw: ImageDraw.Draw, y: int, text: str, font, colour: str, w: int = config.CARD_WIDTH) -> None:
    bbox  = font.getbbox(text)
    tw    = bbox[2] - bbox[0]
    x     = (w - tw) // 2
    draw.text((x, y), text, font=font, fill=_hex(colour))


def _rounded_rect(draw: ImageDraw.Draw, xy, radius: int, fill: str, outline: str | None = None, outline_width: int = 1) -> None:
    draw.rounded_rectangle(xy, radius=radius, fill=_hex(fill),
                           outline=_hex(outline) if outline else None,
                           width=outline_width)


# ---------------------------------------------------------------------------
# Main card generator
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
    """
    Render a 1920×1080 PnL summary card.
    Returns raw PNG bytes suitable for Telegram's send_photo().
    """
    W, H = config.CARD_WIDTH, config.CARD_HEIGHT
    PADDING = 80

    # ---- Canvas ----
    img  = Image.new("RGB", (W, H), _hex(config.COLOUR_BG))
    draw = ImageDraw.Draw(img, "RGBA")

    # ---- Fonts ----
    f_huge   = _get_font(config.FONT_BOLD,     120)
    f_large  = _get_font(config.FONT_BOLD,      60)
    f_medium = _get_font(config.FONT_SEMIBOLD,  40)
    f_small  = _get_font(config.FONT_REGULAR,   32)
    f_tiny   = _get_font(config.FONT_REGULAR,   26)

    # ---- Background gradient effect (horizontal stripe) ----
    accent_r, accent_g, accent_b = _hex(config.COLOUR_ACCENT)
    for y in range(H):
        alpha = int(25 * (1 - y / H))
        draw.line([(0, y), (W, y)], fill=(accent_r, accent_g, accent_b, alpha))

    # ---- Header bar ----
    _rounded_rect(draw, [PADDING, PADDING, W - PADDING, PADDING + 100],
                  radius=16, fill=config.COLOUR_SURFACE, outline=config.COLOUR_BORDER, outline_width=2)

    # Logo (optional)
    if os.path.exists(config.LOGO_PATH):
        try:
            logo = Image.open(config.LOGO_PATH).convert("RGBA")
            logo = logo.resize((70, 70), Image.LANCZOS)
            img.paste(logo, (PADDING + 20, PADDING + 15), logo)
        except Exception:
            pass

    draw.text((PADDING + 110, PADDING + 22), "FundedFrens", font=f_medium, fill=_hex(config.COLOUR_TEXT))
    draw.text((W - PADDING - 340, PADDING + 22), f"@{username}", font=f_medium, fill=_hex(config.COLOUR_MUTED))
    draw.text((W - PADDING - 180, PADDING + 22), f"• {plan_name} Plan", font=f_medium, fill=_hex(config.COLOUR_ACCENT))

    # ---- PnL hero ----
    is_profit = realized_pnl >= 0
    pnl_colour   = config.COLOUR_GREEN if is_profit else config.COLOUR_RED
    sign         = "+" if is_profit else ""
    pnl_text     = f"{sign}{realized_pnl:.4f} SOL"
    pnl_pct_text = f"{sign}{pnl_pct:.2f}%"

    hero_y = PADDING + 130
    _text_center(draw, hero_y, pnl_text, f_huge, pnl_colour)
    _text_center(draw, hero_y + 130, pnl_pct_text, f_large, pnl_colour)
    _text_center(draw, hero_y + 205, "REALIZED PnL", f_small, config.COLOUR_MUTED)

    # ---- Divider ----
    div_y = hero_y + 270
    draw.line([(PADDING, div_y), (W - PADDING, div_y)], fill=_hex(config.COLOUR_BORDER), width=2)

    # ---- Stats grid (2 rows × 4 columns) ----
    stats = [
        ("Start Balance", f"{start_balance:.4f} SOL", config.COLOUR_TEXT),
        ("Current Balance", f"{current_balance:.4f} SOL", pnl_colour),
        ("Win Rate", f"{win_rate:.1f}%", config.COLOUR_GREEN if win_rate >= 50 else config.COLOUR_RED),
        ("Total Trades", str(total_trades), config.COLOUR_TEXT),
        ("Challenge Progress", f"{challenge_progress:.2f}%", config.COLOUR_ACCENT),
        ("Max Drawdown", f"{drawdown:.2f}%", config.COLOUR_RED if drawdown > 5 else config.COLOUR_GREEN),
        ("Trading Days", str(trading_days), config.COLOUR_TEXT),
        ("SOL Price", f"${sol_price:,.2f}", config.COLOUR_MUTED),
    ]

    grid_y  = div_y + 40
    cols    = 4
    cell_w  = (W - PADDING * 2) // cols
    cell_h  = 150

    for i, (label, value, colour) in enumerate(stats):
        row = i // cols
        col = i % cols
        cx  = PADDING + col * cell_w + cell_w // 2
        cy  = grid_y + row * cell_h

        # Card background
        card_x0 = PADDING + col * cell_w + 8
        card_x1 = card_x0 + cell_w - 16
        card_y0 = cy
        card_y1 = cy + cell_h - 12
        _rounded_rect(draw, [card_x0, card_y0, card_x1, card_y1],
                      radius=12, fill=config.COLOUR_SURFACE, outline=config.COLOUR_BORDER, outline_width=1)

        # Value
        val_bbox = f_large.getbbox(value)
        vw = val_bbox[2] - val_bbox[0]
        draw.text((cx - vw // 2, cy + 18), value, font=f_large, fill=_hex(colour))

        # Label
        lbl_bbox = f_tiny.getbbox(label)
        lw = lbl_bbox[2] - lbl_bbox[0]
        draw.text((cx - lw // 2, cy + 88), label, font=f_tiny, fill=_hex(config.COLOUR_MUTED))

    # ---- Footer ----
    footer_y = H - PADDING - 50
    now_str  = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    draw.text((PADDING, footer_y), f"Generated {now_str}", font=f_tiny, fill=_hex(config.COLOUR_MUTED))
    _text_center(draw, footer_y, "fundedfrens.com", f_tiny, config.COLOUR_MUTED)
    draw.text((W - PADDING - 280, footer_y), "Prop Trading Sim Bot", font=f_tiny, fill=_hex(config.COLOUR_MUTED))

    # ---- Watermark glow lines ----
    for i in range(3):
        gy = footer_y - 30 - i * 4
        alpha = 40 - i * 12
        draw.line([(0, gy), (W, gy)], fill=(accent_r, accent_g, accent_b, alpha))

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf.read()
