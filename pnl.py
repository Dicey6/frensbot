"""
pnl.py — matplotlib-based PnL card generator (1920×1080).

Pure Python — no system-level image libraries required.
Uses matplotlib's Agg backend (ships as a wheel, no libjpeg/libpng headers needed).

Called from main.py's /pnl command.
"""

from __future__ import annotations

import io
import logging

import matplotlib
matplotlib.use("Agg")  # must be set before importing pyplot
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import matplotlib.patheffects as pe
from matplotlib.patches import FancyBboxPatch
import numpy as np

import config

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Brand palette
# ---------------------------------------------------------------------------

BG          = "#0A0A0B"
BG2         = "#111114"
SURFACE     = "#18181C"
BORDER      = "#2A2A30"
GREEN       = "#00E676"
RED         = "#FF4560"
TEXT        = "#FFFFFF"
MUTED       = "#6B6B80"
ACCENT      = "#7C4DFF"
ACCENT_DIM  = "#3D2680"

# Figure size → 1920×1080 at 100 dpi
DPI    = 100
FIG_W  = 1920 / DPI   # 19.2
FIG_H  = 1080 / DPI   # 10.8


# ---------------------------------------------------------------------------
# Drawing helpers
# ---------------------------------------------------------------------------

def _hex_to_rgb01(h: str) -> tuple[float, float, float]:
    h = h.lstrip("#")
    return tuple(int(h[i:i+2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]


def _add_glow(ax, x: float, y: float, text: str, size: float,
              color: str, ha: str = "center", va: str = "center",
              weight: str = "bold", alpha_glow: float = 0.25) -> None:
    """Draw text with a soft colour glow behind it."""
    # glow layers
    for spread in (8, 5, 3):
        ax.text(x, y, text, ha=ha, va=va, fontsize=size, fontweight=weight,
                color=color, alpha=alpha_glow,
                path_effects=[pe.withStroke(linewidth=spread, foreground=color)],
                transform=ax.transAxes)
    ax.text(x, y, text, ha=ha, va=va, fontsize=size, fontweight=weight,
            color=color, transform=ax.transAxes)


def _stat_card(ax, x: float, y: float, w: float, h: float,
               label: str, value: str, value_color: str) -> None:
    """Draw a single stat tile (axes-fraction coords)."""
    # Background tile
    rect = FancyBboxPatch(
        (x, y), w, h,
        boxstyle="round,pad=0.005",
        linewidth=0.8,
        edgecolor=BORDER,
        facecolor=SURFACE,
        transform=ax.transAxes,
        zorder=2,
    )
    ax.add_patch(rect)

    # Accent left edge bar
    bar = FancyBboxPatch(
        (x, y), 0.003, h,
        boxstyle="round,pad=0",
        linewidth=0,
        facecolor=ACCENT,
        alpha=0.6,
        transform=ax.transAxes,
        zorder=3,
    )
    ax.add_patch(bar)

    cx = x + w / 2
    # Value text
    ax.text(cx, y + h * 0.62, value,
            ha="center", va="center",
            fontsize=11, fontweight="bold", color=value_color,
            transform=ax.transAxes, zorder=4)
    # Label text
    ax.text(cx, y + h * 0.25, label,
            ha="center", va="center",
            fontsize=7.5, color=MUTED,
            transform=ax.transAxes, zorder=4)


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
    from datetime import datetime, timezone

    is_profit   = realized_pnl >= 0
    pnl_colour  = GREEN if is_profit else RED
    sign        = "+" if is_profit else ""
    pnl_text    = f"{sign}{realized_pnl:.4f} SOL"
    pnl_pct_txt = f"{sign}{pnl_pct:.2f}%"

    # ---- Figure setup -------------------------------------------------------
    fig = plt.figure(figsize=(FIG_W, FIG_H), dpi=DPI, facecolor=BG)
    ax  = fig.add_axes([0, 0, 1, 1])   # full bleed
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")
    ax.set_facecolor(BG)

    # ---- Subtle background gradient (top accent glow) ----------------------
    grad_data = np.linspace(0.12, 0, 256).reshape(256, 1)
    ax.imshow(
        np.ones((256, 1, 4)) * np.array([*_hex_to_rgb01(ACCENT), 1]) * grad_data[:, :, np.newaxis],  # type: ignore[call-overload]
        extent=[0, 1, 0, 1], aspect="auto", origin="upper", zorder=0,
    )

    # ---- Header bar ---------------------------------------------------------
    header = FancyBboxPatch(
        (0.03, 0.88), 0.94, 0.09,
        boxstyle="round,pad=0.005",
        linewidth=1,
        edgecolor=BORDER,
        facecolor=SURFACE,
        transform=ax.transAxes, zorder=2,
    )
    ax.add_patch(header)

    # Brand name left
    ax.text(0.06, 0.925, "FundedFrens", ha="left", va="center",
            fontsize=18, fontweight="bold", color=TEXT,
            transform=ax.transAxes, zorder=3)

    # Accent dot separator
    ax.text(0.5, 0.925, "◆", ha="center", va="center",
            fontsize=10, color=ACCENT, alpha=0.7,
            transform=ax.transAxes, zorder=3)

    # Username + plan right
    ax.text(0.94, 0.935, f"@{username}", ha="right", va="center",
            fontsize=13, fontweight="bold", color=TEXT,
            transform=ax.transAxes, zorder=3)
    ax.text(0.94, 0.9, f"{plan_name} Plan", ha="right", va="center",
            fontsize=10, color=ACCENT,
            transform=ax.transAxes, zorder=3)

    # ---- Hero PnL -----------------------------------------------------------
    _add_glow(ax, 0.5, 0.72, pnl_text,    size=54, color=pnl_colour,
              alpha_glow=0.18)
    _add_glow(ax, 0.5, 0.62, pnl_pct_txt, size=30, color=pnl_colour,
              alpha_glow=0.14)
    ax.text(0.5, 0.555, "REALIZED PnL", ha="center", va="center",
            fontsize=11, color=MUTED, letterspacing=4,
            transform=ax.transAxes, zorder=3)

    # ---- Divider line -------------------------------------------------------
    ax.axhline(y=0.53, xmin=0.03, xmax=0.97,
               color=BORDER, linewidth=1, zorder=2)

    # Tiny accent sparkle on divider
    ax.plot(0.5, 0.53, "o", color=ACCENT, markersize=5, zorder=3)

    # ---- Stats grid (2 rows × 4 cols) ---------------------------------------
    stats = [
        ("Start Balance",       f"{start_balance:.4f} SOL",   TEXT),
        ("Current Balance",     f"{current_balance:.4f} SOL",  pnl_colour),
        ("Win Rate",            f"{win_rate:.1f}%",            GREEN if win_rate >= 50 else RED),
        ("Total Trades",        str(total_trades),             TEXT),
        ("Challenge Progress",  f"{challenge_progress:.2f}%",  ACCENT),
        ("Max Drawdown",        f"{drawdown:.2f}%",            RED if drawdown > 5 else GREEN),
        ("Trading Days",        str(trading_days),             TEXT),
        ("SOL Price",           f"${sol_price:,.2f}",          MUTED),
    ]

    cols   = 4
    PAD_X  = 0.03
    PAD_Y  = 0.035
    GRID_X = PAD_X
    GRID_Y = 0.055          # bottom of grid area
    GRID_W = 1 - PAD_X * 2
    GRID_H = 0.455          # height of both rows

    cell_w = GRID_W / cols
    cell_h = GRID_H / 2

    INNER_PAD = 0.005

    for i, (label, value, colour) in enumerate(stats):
        row = i // cols
        col = i % cols
        x   = GRID_X + col * cell_w + INNER_PAD
        y   = GRID_Y + (1 - row) * cell_h - cell_h + INNER_PAD
        w   = cell_w - INNER_PAD * 2
        h   = cell_h - INNER_PAD * 2
        _stat_card(ax, x, y, w, h, label, value, colour)

    # ---- Footer -------------------------------------------------------------
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    ax.text(0.04, 0.025, f"Generated {now_str}",
            ha="left", va="center", fontsize=8, color=MUTED,
            transform=ax.transAxes, zorder=3)
    ax.text(0.5, 0.025, "fundedfrens.com",
            ha="center", va="center", fontsize=8, color=MUTED, alpha=0.6,
            transform=ax.transAxes, zorder=3)
    ax.text(0.96, 0.025, "Prop Trading Sim",
            ha="right", va="center", fontsize=8, color=MUTED,
            transform=ax.transAxes, zorder=3)

    # Subtle footer accent line
    ax.axhline(y=0.045, xmin=0.03, xmax=0.97,
               color=BORDER, linewidth=0.6, alpha=0.5, zorder=2)

    # ---- Render to PNG bytes ------------------------------------------------
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=DPI, bbox_inches="tight",
                facecolor=BG, edgecolor="none")
    plt.close(fig)
    buf.seek(0)
    return buf.read()
