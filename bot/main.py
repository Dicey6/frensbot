"""
main.py — FundedFrens Telegram Trading Bot

Commands: /start /home /positions /portfolio /challenge /settings /help /pnl

Design:
  - Messages always sent fresh — history is never edited or deleted.
  - Refresh actions EDIT the current message in-place rather than sending new.
  - No confirmation steps on buy or sell — tap to execute instantly.
  - No auto-sell — only manual sells, stop-loss, and trailing stop.
  - Website username (profiles.username) is used everywhere.
  - Supabase is the single source of truth.
"""

from __future__ import annotations

import logging
import re
import sys
from datetime import datetime, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import config
import database as db
import trading
from config import GAS_FEE_SOL, TRADING
from pnl import generate_pnl_card, generate_position_card

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Conversation states
# ---------------------------------------------------------------------------

BUY_CUSTOM_AMOUNT, SELL_CUSTOM_PCT, SETTINGS_VALUE = range(3)

# ---------------------------------------------------------------------------
# Keyboards
# ---------------------------------------------------------------------------


def _home_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📊 Positions", callback_data="positions"),
            InlineKeyboardButton("💼 Portfolio", callback_data="portfolio"),
        ],
        [
            InlineKeyboardButton("🏆 Challenge", callback_data="challenge"),
            InlineKeyboardButton("⚙️ Settings",  callback_data="settings"),
        ],
        [
            InlineKeyboardButton("🎴 PnL Card",   callback_data="cmd_pnl"),
            InlineKeyboardButton("📋 Commands",   callback_data="commands"),
        ],
    ])


def _commands_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📊 Positions",  callback_data="positions"),
            InlineKeyboardButton("💼 Portfolio",  callback_data="portfolio"),
        ],
        [
            InlineKeyboardButton("🏆 Challenge",  callback_data="challenge"),
            InlineKeyboardButton("⚙️ Settings",   callback_data="settings"),
        ],
        [
            InlineKeyboardButton("🎴 PnL Card",   callback_data="cmd_pnl"),
            InlineKeyboardButton("❓ Help",        callback_data="help"),
        ],
        [InlineKeyboardButton("🏠 Home",           callback_data="home")],
    ])


def _back(to: str = "home", label: str = "← Back") -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(label, callback_data=to)]


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------


async def _show(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    keyboard: InlineKeyboardMarkup | None = None,
) -> None:
    """Always sends a new message."""
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=text,
        parse_mode="Markdown",
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )


async def _edit_or_show(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    keyboard: InlineKeyboardMarkup | None = None,
) -> None:
    """
    Edits the current message in-place when called from a callback query
    (refresh actions), otherwise sends a new message.
    Falls back to send_message if the edit fails for any reason.
    """
    q = update.callback_query
    if q and q.message:
        try:
            await q.edit_message_text(
                text=text,
                parse_mode="Markdown",
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )
            return
        except Exception as e:
            log.debug("edit_message_text failed, falling back to send: %s", e)
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=text,
        parse_mode="Markdown",
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )


def _uid(update: Update) -> int:
    return update.effective_user.id


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def _fmt_pct(pct: float) -> str:
    return f"+{pct:.2f}%" if pct >= 0 else f"{pct:.2f}%"


def _fmt_sol(sol: float) -> str:
    return f"{sol:.4f} SOL"


def _pnl_emoji(val: float) -> str:
    return "🟢" if val >= 0 else "🔴"


def _time_ago(dt_str: str) -> str:
    try:
        opened = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        secs   = int((datetime.now(timezone.utc) - opened).total_seconds())
        if secs < 60:    return f"{secs}s"
        if secs < 3600:  return f"{secs // 60}m"
        if secs < 86400:
            h, m = divmod(secs, 3600)
            return f"{h}h {m // 60}m"
        return f"{secs // 86400}d"
    except Exception:
        return "—"


def _hold_time_str(dt_str: str) -> str:
    try:
        opened = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        secs   = int((datetime.now(timezone.utc) - opened).total_seconds())
        h, rem = divmod(secs, 3600)
        m      = rem // 60
        return f"{h}h {m}m" if h > 0 else (f"{m}m" if m > 0 else f"{secs}s")
    except Exception:
        return "—"


def _fmt_price(usd: float) -> str:
    if usd <= 0:          return "$0.00"
    if usd < 0.000001:    return f"${usd:.10f}"
    if usd < 0.001:       return f"${usd:.8f}"
    if usd < 1:           return f"${usd:.6f}"
    if usd < 1000:        return f"${usd:.4f}"
    return f"${usd:,.2f}"


def _fmt_mc(usd: float) -> str:
    if usd >= 1_000_000_000: return f"${usd / 1_000_000_000:.2f}B"
    if usd >= 1_000_000:     return f"${usd / 1_000_000:.2f}M"
    if usd >= 1_000:         return f"${usd / 1_000:.1f}K"
    return f"${usd:.0f}"


def _risk_str(value, suffix: str = "%") -> str:
    if value is None:
        return "Not Set"
    try:
        v = float(value)
        return "Not Set" if v == 0 else f"{v:.0f}{suffix}"
    except (ValueError, TypeError):
        return "Not Set"


def _to_float_or_none(v) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
        return f if f != 0 else None
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Auth guards
# ---------------------------------------------------------------------------


async def _linked_profile(telegram_id: int) -> dict | None:
    p = await db.get_profile_by_telegram_id(telegram_id)
    return p if (p and p.get("telegram_linked")) else None


async def _profile_and_challenge(telegram_id: int) -> tuple[dict | None, dict | None]:
    p = await _linked_profile(telegram_id)
    if not p:
        return None, None
    c = await db.get_active_challenge(p["id"])
    return p, c


# ---------------------------------------------------------------------------
# Unlinked / no challenge
# ---------------------------------------------------------------------------


async def _show_not_linked(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _show(
        update, context,
        "🏆 *FundedFrens Trading Terminal*\n\n"
        "To get started, link your account:\n\n"
        "1. Go to *fundedfrens.com*\n"
        "2. Profile → Settings → copy your *Telegram Link Code*\n"
        "3. Paste it here\n\n"
        "Format: `TG-XXXXXXXXXX`",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("🌐 fundedfrens.com", url="https://fundedfrens.com")],
            [InlineKeyboardButton("❓ Help", callback_data="help")],
        ]),
    )


async def _show_no_challenge(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _show(
        update, context,
        f"⚠️ *No Active Challenge*\n\nVisit {config.APP_URL} to purchase or activate a challenge.",
        InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Refresh", callback_data="home")]]),
    )


# ---------------------------------------------------------------------------
# Home
# ---------------------------------------------------------------------------


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    profile = await _linked_profile(_uid(update))
    if profile:
        await _show_home(update, context, profile)
    else:
        await _show_not_linked(update, context)


async def cmd_home(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    profile = await _linked_profile(_uid(update))
    if not profile:
        await _show_not_linked(update, context)
        return
    await _show_home(update, context, profile)


async def _show_home(update: Update, context: ContextTypes.DEFAULT_TYPE, profile: dict) -> None:
    challenge = await db.get_active_challenge(profile["id"])
    if not challenge:
        await _show_no_challenge(update, context)
        return

    summary  = await db.get_account_summary(profile["id"], challenge)
    plan     = (challenge.get("challenge_plan") or "Starter").title()
    open_pos = int(challenge.get("open_positions") or 0)
    drawdown = float(challenge.get("drawdown") or summary["drawdown_pct"])
    username = profile.get("username") or "Trader"
    title    = "🏆 *Funded Trader*" if challenge.get("is_funded") else "🏆 *FundedFrens*"

    await _show(
        update, context,
        f"{title}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"*Trader:* {username}  |  *Plan:* {plan}\n\n"
        f"*Challenge Capital*\n`{_fmt_sol(summary['start_balance'])}`\n\n"
        f"*Buying Power*\n`{_fmt_sol(summary['available_sol'])}`\n\n"
        f"*Open Positions:* `{open_pos} / {TRADING.max_open_positions}`\n"
        f"*Drawdown:* `{drawdown:.2f}%`\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"_Paste a contract address, ticker, or token link to trade._",
        _home_keyboard(),
    )


# ---------------------------------------------------------------------------
# Account linking
# ---------------------------------------------------------------------------


async def _do_link(update: Update, context: ContextTypes.DEFAULT_TYPE, code: str) -> None:
    code = code.strip().upper()
    if not code.startswith("TG-"):
        await _show(update, context, "❌ That doesn't look like a link code.\n\nFormat: `TG-XXXXXXXXXX`")
        return

    profile = await db.get_profile_by_link_code(code)
    if not profile:
        await _show(
            update, context,
            "❌ *Code not found or already used.*\n\n"
            "• Double-check the code on the website\n"
            "• Each code can only link one account",
        )
        return

    user    = update.effective_user
    success = await db.link_telegram(profile["id"], user.id, user.username)
    if not success:
        await _show(update, context, "⚠️ Something went wrong. Please try again.")
        return

    await _show(
        update, context,
        f"✅ *Account linked!*\n\n"
        f"Welcome, *{profile.get('username') or 'Trader'}* 🎉\n\n"
        f"Paste any contract address, ticker, or link to start trading.",
        _home_keyboard(),
    )


# ---------------------------------------------------------------------------
# Token page
# ---------------------------------------------------------------------------


async def _show_token_page(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    address: str,
    profile: dict,
    challenge: dict,
    *,
    use_edit: bool = False,
) -> None:
    token = await trading.get_full_token_info(address)
    if not token:
        msg_fn = _edit_or_show if use_edit else _show
        await msg_fn(
            update, context,
            "❌ *Token not found.*\n\nCheck the address or try a different one.",
            InlineKeyboardMarkup([_back()]),
        )
        return

    context.user_data["current_token"] = token
    context.user_data["profile"]       = profile
    context.user_data["challenge"]     = challenge

    summary  = await db.get_account_summary(profile["id"], challenge)
    settings = await db.get_bot_settings(profile["id"])
    qb1 = float(settings.get("quick_buy_1") or TRADING.quick_buy_1)
    qb2 = float(settings.get("quick_buy_2") or TRADING.quick_buy_2)
    qb3 = float(settings.get("quick_buy_3") or TRADING.quick_buy_3)

    # Use the resolved address from token info (handles pair-address lookups)
    resolved_addr = token.get("address") or address
    short        = f"{resolved_addr[:6]}...{resolved_addr[-4:]}"
    chart_url    = token.get("dex_url") or f"https://dexscreener.com/solana/{resolved_addr}"
    explorer_url = f"https://solscan.io/token/{resolved_addr}"
    scan_url     = f"https://rugcheck.xyz/tokens/{resolved_addr}"
    plan         = (challenge.get("challenge_plan") or "Starter").title()
    open_pos     = int(challenge.get("open_positions") or 0)
    drawdown     = float(challenge.get("drawdown") or summary["drawdown_pct"])

    # ── Bonding curve line (only for pump.fun tokens still on the curve)
    curve_pct = token.get("bonding_curve")
    if curve_pct is not None:
        bar_filled = int(curve_pct / 5)           # 0-20 blocks
        bar_empty  = 20 - bar_filled
        curve_bar  = "█" * bar_filled + "░" * bar_empty
        curve_line = f"*Bonding Curve:* `{curve_bar}` `{curve_pct:.1f}%`\n"
    else:
        curve_line = ""

    # ── Mint authority / renounced status line
    renounced = token.get("renounced")
    if renounced is True:
        authority_line = f"*Mint Authority:* Renounced ✅\n"
    elif renounced is False:
        authority_line = f"*Mint Authority:* Active ⚠️\n"
    else:
        authority_line = ""    # Helius unavailable — omit silently

    text = (
        f"*{token['name']}*  `{token['symbol']}`\n"
        f"`{short}`\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"*Price:* {_fmt_price(token['price_usd'])}\n"
        f"5m `{_fmt_pct(token['change_5m'])}` · 1h `{_fmt_pct(token['change_1h'])}`\n"
        f"6h `{_fmt_pct(token['change_6h'])}` · 24h `{_fmt_pct(token['change_24h'])}`\n\n"
        f"*Market Cap:* `{_fmt_mc(token['market_cap'])}`\n"
        f"*Liquidity:* `{_fmt_mc(token['liquidity_usd'])}`\n"
        f"*24h Volume:* `{_fmt_mc(token['volume_24h'])}`\n"
        f"*Price Impact:* `{token['price_impact']}`\n"
        f"{curve_line}"
        f"{authority_line}"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"*Plan:* {plan}  |  *Positions:* `{open_pos}/{TRADING.max_open_positions}`\n"
        f"*Buying Power:* `{_fmt_sol(summary['available_sol'])}`  |  "
        f"*Drawdown:* `{drawdown:.2f}%`"
    )

    msg_fn = _edit_or_show if use_edit else _show
    await msg_fn(
        update, context, text,
        InlineKeyboardMarkup([
            [
                InlineKeyboardButton(f"Buy {qb1} SOL", callback_data="buy_q1"),
                InlineKeyboardButton(f"Buy {qb2} SOL", callback_data="buy_q2"),
                InlineKeyboardButton(f"Buy {qb3} SOL", callback_data="buy_q3"),
            ],
            [InlineKeyboardButton("Buy X SOL",  callback_data="buy_custom")],
            [InlineKeyboardButton("🔄 Refresh", callback_data="token_refresh")],
            [
                InlineKeyboardButton("🔍 Explorer", url=explorer_url),
                InlineKeyboardButton("📈 Chart",    url=chart_url),
                InlineKeyboardButton("🛡 Scan",     url=scan_url),
            ],
            _back(),
        ]),
    )


# ---------------------------------------------------------------------------
# Positions list
# ---------------------------------------------------------------------------

SORT_LABELS = {
    "profit": "Highest Profit", "loss": "Highest Loss",
    "newest": "Newest",         "oldest": "Oldest",
    "largest": "Largest",       "alpha": "A-Z",
}


async def cmd_positions(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    sort: str = "newest",
) -> None:
    profile = await _linked_profile(_uid(update))
    if not profile:
        await _show_not_linked(update, context)
        return

    challenge = await db.get_active_challenge(profile["id"])
    summary   = await db.get_account_summary(profile["id"], challenge) if challenge else None
    positions = await db.get_open_positions(profile["id"])

    if not positions:
        await _show(
            update, context,
            "📊 *Open Positions*\n\n━━━━━━━━━━━━━━━━━━━━\n"
            "No open positions.\n\n_Paste a contract address or ticker to start trading._",
            InlineKeyboardMarkup([_back()]),
        )
        return

    import asyncio as _asyncio
    sol_price = await db.fetch_sol_price()
    pairs = await _asyncio.gather(*[trading.get_token_price(p["token_address"]) for p in positions])

    enriched = []
    for pos, pair in zip(positions, pairs):
        current = trading.price_in_sol(pair, sol_price) if pair else float(pos["entry_price_sol"])
        entry   = float(pos["entry_price_sol"])
        inv     = float(pos["amount_sol_invested"])
        pnl_pct = (current - entry) / entry * 100 if entry > 0 else 0
        pnl_sol = inv * pnl_pct / 100
        enriched.append({**pos, "_pnl_sol": pnl_sol, "_pnl_pct": pnl_pct, "_cur_val": inv + pnl_sol})

    if sort == "profit":   enriched.sort(key=lambda x: x["_pnl_sol"], reverse=True)
    elif sort == "loss":   enriched.sort(key=lambda x: x["_pnl_sol"])
    elif sort == "newest": enriched.sort(key=lambda x: x.get("opened_at", ""), reverse=True)
    elif sort == "oldest": enriched.sort(key=lambda x: x.get("opened_at", ""))
    elif sort == "largest":enriched.sort(key=lambda x: x["_cur_val"], reverse=True)
    elif sort == "alpha":  enriched.sort(key=lambda x: x["token_symbol"])

    total_val     = sum(p["_cur_val"] for p in enriched)
    total_pnl     = sum(p["_pnl_sol"] for p in enriched)
    cost_basis    = total_val - total_pnl
    total_pnl_pct = (total_pnl / cost_basis * 100) if cost_basis > 0 else 0
    pnl_sign      = "+" if total_pnl >= 0 else ""
    buying_power  = summary["available_sol"] if summary else 0.0

    lines = [
        f"📊 *Open Positions*  `{len(enriched)} / {TRADING.max_open_positions}`\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"*Total Value:* `{_fmt_sol(total_val)}`\n"
        f"*Unrealized PnL:* {_pnl_emoji(total_pnl)} "
        f"`{pnl_sign}{_fmt_sol(total_pnl)}` (`{pnl_sign}{total_pnl_pct:.2f}%`)\n"
        f"*Buying Power:* `{_fmt_sol(buying_power)}`\n"
        f"━━━━━━━━━━━━━━━━━━━━"
    ]
    rows: list[list[InlineKeyboardButton]] = []

    for p in enriched:
        sign  = "+" if p["_pnl_sol"] >= 0 else ""
        emoji = _pnl_emoji(p["_pnl_sol"])
        lines.append(
            f"\n{emoji} *{p['token_symbol']}*\n"
            f"`{sign}{p['_pnl_pct']:.2f}%`  ·  `{_fmt_sol(p['_cur_val'])}`"
        )
        rows.append([InlineKeyboardButton(f"View {p['token_symbol']}", callback_data=f"pos_view_{p['id']}")])

    rows.append([InlineKeyboardButton(f"⇅ Sort: {SORT_LABELS.get(sort, 'Newest')}", callback_data="pos_sort_menu")])
    rows.append(_back())
    context.user_data["positions_sort"] = sort
    await _show(update, context, "\n".join(lines), InlineKeyboardMarkup(rows))


async def _show_sort_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _show(
        update, context,
        "⇅ *Sort Positions*\n\nChoose sort order:",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("📈 Highest Profit",   callback_data="pos_sort_profit")],
            [InlineKeyboardButton("📉 Highest Loss",     callback_data="pos_sort_loss")],
            [InlineKeyboardButton("🆕 Newest",           callback_data="pos_sort_newest")],
            [InlineKeyboardButton("🕰 Oldest",           callback_data="pos_sort_oldest")],
            [InlineKeyboardButton("💰 Largest Position", callback_data="pos_sort_largest")],
            [InlineKeyboardButton("🔤 Alphabetical",     callback_data="pos_sort_alpha")],
            _back("positions"),
        ]),
    )


# ---------------------------------------------------------------------------
# Position screen
# ---------------------------------------------------------------------------


async def _show_position_screen(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    position_id: int,
    *,
    use_edit: bool = False,
) -> None:
    position = await db.get_position(position_id)
    if not position:
        await _show(update, context, "❌ Position not found.", InlineKeyboardMarkup([_back("positions")]))
        return

    profile = await _linked_profile(_uid(update))
    if not profile:
        await _show_not_linked(update, context)
        return

    address   = position["token_address"]
    is_closed = (position.get("status") or "open") == "closed"

    sol_price = await db.fetch_sol_price()
    pair      = await trading.get_token_price(address)
    current   = trading.price_in_sol(pair, sol_price) if pair else float(position["entry_price_sol"])
    entry     = float(position["entry_price_sol"])
    inv       = float(position["amount_sol_invested"])
    pnl_pct   = (current - entry) / entry * 100 if entry > 0 else 0
    pnl_sol   = inv * pnl_pct / 100
    cur_val   = inv + pnl_sol
    sign      = "+" if pnl_sol >= 0 else ""
    emoji     = _pnl_emoji(pnl_sol)
    pnl_usd   = pnl_sol * sol_price

    # Market cap values (shown instead of raw price)
    entry_mc   = float(position.get("entry_market_cap_usd") or 0)
    current_mc = float((pair.get("marketCap") or pair.get("fdv") or 0)) if pair else 0
    liq        = float(((pair.get("liquidity") or {}).get("usd") or 0)) if pair else 0
    vol        = float(((pair.get("volume") or {}).get("h24") or 0)) if pair else 0
    chart_url  = (pair.get("url") if pair else None) or f"https://dexscreener.com/solana/{address}"

    # Full CA is always shown, ready to copy
    text = (
        f"*{position['token_symbol']}*  {emoji}"
        + (" `[CLOSED]`" if is_closed else "") + "\n"
        f"`{address}`\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"*Entry MC:* `{_fmt_mc(entry_mc) if entry_mc > 0 else '—'}`  →  "
        f"*Now MC:* `{_fmt_mc(current_mc) if current_mc > 0 else '—'}`\n"
        f"*Invested:* `{_fmt_sol(inv)}`  |  *Value:* `{_fmt_sol(cur_val)}`\n"
        f"*PnL:* {emoji} `{sign}{_fmt_sol(pnl_sol)}` (`{sign}{pnl_pct:.2f}%`)  "
        f"`{sign}{_fmt_price(pnl_usd)}`\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"*MC:* `{_fmt_mc(current_mc)}`  |  *Liq:* `{_fmt_mc(liq)}`  |  *Vol:* `{_fmt_mc(vol)}`\n"
        f"*Open:* `{_time_ago(position.get('opened_at', ''))}`\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"SL: `{_risk_str(position.get('stop_loss_pct'))}`  "
        f"TP: `{_risk_str(position.get('take_profit_pct'))}`  "
        f"Trail: `{_risk_str(position.get('trailing_stop_pct'))}`"
    )

    if is_closed:
        # Position fully sold — no sell buttons
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🎴 PnL Card", callback_data=f"pos_pnl_{position_id}"),
            ],
            [
                InlineKeyboardButton("📈 Chart",    url=chart_url),
                InlineKeyboardButton("🔍 Explorer", url=f"https://solscan.io/token/{address}"),
            ],
            _back("positions"),
        ])
    else:
        settings = await db.get_bot_settings(profile["id"])
        qs1 = float(settings.get("quick_sell_1") or TRADING.quick_sell_1)
        qs2 = float(settings.get("quick_sell_2") or TRADING.quick_sell_2)
        qs3 = float(settings.get("quick_sell_3") or TRADING.quick_sell_3)

        context.user_data["current_position_id"] = position_id
        context.user_data["profile"]             = profile

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(f"Sell {qs1:.0f}%", callback_data="pos_sell_q1"),
                InlineKeyboardButton(f"Sell {qs2:.0f}%", callback_data="pos_sell_q2"),
            ],
            [
                InlineKeyboardButton(f"Sell {qs3:.0f}%", callback_data="pos_sell_q3"),
                InlineKeyboardButton("Sell X%",           callback_data="pos_sell_custom"),
            ],
            [
                InlineKeyboardButton("🎴 PnL Card", callback_data=f"pos_pnl_{position_id}"),
                InlineKeyboardButton("🔄 Refresh",  callback_data=f"pos_refresh_{position_id}"),
            ],
            [
                InlineKeyboardButton("📈 Chart",    url=chart_url),
                InlineKeyboardButton("🔍 Explorer", url=f"https://solscan.io/token/{address}"),
            ],
            _back("positions"),
        ])

    msg_fn = _edit_or_show if use_edit else _show
    await msg_fn(update, context, text, keyboard)


# ---------------------------------------------------------------------------
# PnL card from position (sent as additional photo — screen stays visible)
# ---------------------------------------------------------------------------


async def _send_position_pnl_card(update: Update, context: ContextTypes.DEFAULT_TYPE, position_id: int) -> None:
    position = await db.get_position(position_id)
    if not position:
        return

    profile, challenge = await _profile_and_challenge(_uid(update))
    if not profile:
        return

    sol_price = await db.fetch_sol_price()
    pair      = await trading.get_token_price(position["token_address"])
    current   = trading.price_in_sol(pair, sol_price) if pair else float(position["entry_price_sol"])
    entry     = float(position["entry_price_sol"])
    inv       = float(position["amount_sol_invested"])
    pnl_pct   = (current - entry) / entry * 100 if entry > 0 else 0
    pnl_sol   = inv * pnl_pct / 100

    img = generate_position_card(
        username=profile.get("username") or "Trader",
        plan_name=(challenge.get("challenge_plan") or "starter").title() if challenge else "—",
        token_symbol=position["token_symbol"],
        token_name=position.get("token_name") or position["token_symbol"],
        token_address=position["token_address"],
        entry_price_usd=entry * sol_price,
        current_price_usd=current * sol_price,
        amount_sol_invested=inv,
        current_value_sol=inv + pnl_sol,
        pnl_sol=pnl_sol,
        pnl_pct=pnl_pct,
        entry_market_cap_usd=float(position.get("entry_market_cap_usd") or 0) or None,
        current_market_cap_usd=float((pair.get("marketCap") or pair.get("fdv") or 0)) if pair else None,
        liquidity_usd=float(((pair.get("liquidity") or {}).get("usd") or 0)) if pair else None,
        hold_time_str=_hold_time_str(position.get("opened_at", "")),
        sol_price=sol_price,
    )

    sign = "+" if pnl_sol >= 0 else ""
    pnl_usd = pnl_sol * sol_price
    await context.bot.send_photo(
        chat_id=update.effective_chat.id,
        photo=img,
        caption=(
            f"🎴 *{position['token_symbol']}*  "
            f"{sign}{pnl_sol:.4f} SOL  ({sign}{pnl_pct:.2f}%)  "
            f"{sign}{_fmt_price(pnl_usd)}"
        ),
        parse_mode="Markdown",
    )


# ---------------------------------------------------------------------------
# Portfolio
# ---------------------------------------------------------------------------


async def cmd_portfolio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    profile = await _linked_profile(_uid(update))
    if not profile:
        await _show_not_linked(update, context)
        return
    challenge = await db.get_active_challenge(profile["id"])
    if not challenge:
        await _show_no_challenge(update, context)
        return

    import asyncio as _asyncio
    summary = await db.get_account_summary(profile["id"], challenge)
    trades  = await db.get_trades(profile["id"], challenge_id=challenge["id"], limit=500)

    sell_trades  = [t for t in trades if t.get("side") == "sell"]
    winners      = sum(1 for t in sell_trades if float(t.get("pnl_sol") or 0) > 0)
    win_rate     = winners / len(sell_trades) * 100 if sell_trades else 0.0
    losers       = len(sell_trades) - winners

    closed_pos   = await db.get_closed_positions(profile["id"], limit=100)
    avg_hold_str = "—"
    hold_times   = []
    for p in closed_pos:
        if p.get("opened_at") and p.get("closed_at"):
            try:
                o = datetime.fromisoformat(p["opened_at"].replace("Z", "+00:00"))
                c = datetime.fromisoformat(p["closed_at"].replace("Z", "+00:00"))
                hold_times.append((c - o).total_seconds())
            except Exception:
                pass
    if hold_times:
        avg = sum(hold_times) / len(hold_times)
        h, rem = divmod(int(avg), 3600)
        avg_hold_str = f"{h}h {rem // 60}m" if h > 0 else f"{rem // 60}m"

    sol_price  = summary["sol_price"]
    positions  = await db.get_open_positions(profile["id"])
    pairs      = await _asyncio.gather(*[trading.get_token_price(p["token_address"]) for p in positions])
    unreal_pnl = 0.0
    for pos, pair in zip(positions, pairs):
        current = trading.price_in_sol(pair, sol_price) if pair else float(pos["entry_price_sol"])
        entry   = float(pos["entry_price_sol"])
        inv     = float(pos["amount_sol_invested"])
        if entry > 0:
            unreal_pnl += inv * (current - entry) / entry

    rpnl     = summary["realized_pnl"]
    rpnl_s   = "+" if rpnl >= 0 else ""
    unreal_s = "+" if unreal_pnl >= 0 else ""
    progress = float(challenge.get("challenge_progress") or 0)
    bar_fill = int(min(progress / 10, 10))
    prog_bar = "█" * bar_fill + "░" * (10 - bar_fill)

    text = (
        f"💼 *Portfolio*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"*Plan:* {(challenge.get('challenge_plan') or 'Starter').title()}\n"
        f"*Realized PnL:* {_pnl_emoji(rpnl)} `{rpnl_s}{_fmt_sol(rpnl)}` (`{rpnl_s}{summary['pnl_pct']:.2f}%`)\n"
        f"*Unrealized PnL:* {_pnl_emoji(unreal_pnl)} `{unreal_s}{_fmt_sol(unreal_pnl)}`\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"*Win Rate:* `{win_rate:.1f}%`  |  *W:* `{winners}`  |  *L:* `{losers}`\n"
        f"*Avg Hold Time:* `{avg_hold_str}`\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"*Challenge Progress:* `{progress:.2f}%`\n"
        f"`{prog_bar}` `{progress:.1f}%`\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"*Recent Trades*\n"
    )
    for t in sell_trades[:8]:
        pnl  = float(t.get("pnl_sol") or 0)
        s    = "+" if pnl >= 0 else ""
        date = (t.get("created_at") or "")[:10]
        text += f"{_pnl_emoji(pnl)} *{t['token_symbol']}*  `{s}{_fmt_sol(pnl)}`  _{date}_\n"
    if not sell_trades:
        text += "_No closed trades yet._"

    await _show(update, context, text, InlineKeyboardMarkup([_back()]))


# ---------------------------------------------------------------------------
# Challenge
# ---------------------------------------------------------------------------


async def cmd_challenge(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    profile = await _linked_profile(_uid(update))
    if not profile:
        await _show_not_linked(update, context)
        return
    challenge = await db.get_active_challenge(profile["id"])
    if not challenge:
        await _show_no_challenge(update, context)
        return

    summary      = await db.get_account_summary(profile["id"], challenge)
    plan         = (challenge.get("challenge_plan") or "Starter").title()
    open_pos     = int(challenge.get("open_positions") or 0)
    trading_days = int(challenge.get("trading_days") or 0)
    drawdown     = float(challenge.get("drawdown") or summary["drawdown_pct"])
    win_rate     = float(challenge.get("win_rate") or 0)
    progress     = float(challenge.get("challenge_progress") or 0)
    bar_fill     = int(min(progress / 10, 10))
    prog_bar     = "█" * bar_fill + "░" * (10 - bar_fill)

    await _show(
        update, context,
        f"{'🏆 *Funded Dashboard*' if challenge.get('is_funded') else '🏆 *Challenge Account*'}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"*Plan:* {plan}  |  *Status:* 🟢 Active\n"
        f"*Capital:* `{_fmt_sol(summary['start_balance'])}`  (~${summary['plan_usd']:,.0f})\n"
        f"*Buying Power:* `{_fmt_sol(summary['available_sol'])}`\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"*Open Positions:* `{open_pos} / {TRADING.max_open_positions}`\n"
        f"*Trading Days:* `{trading_days}`\n"
        f"*Win Rate:* `{win_rate:.1f}%`\n"
        f"*Drawdown:* `{drawdown:.2f}%`  _(max {TRADING.max_drawdown_pct:.0f}%)_\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"*Progress:* `{progress:.2f}%` / 10%\n"
        f"`{prog_bar}` `{progress:.1f}%`\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"*Rules*\n"
        f"• Profit Target: `10%`\n"
        f"• Max Drawdown: `{TRADING.max_drawdown_pct:.0f}%`\n"
        f"• Max Positions: `{TRADING.max_open_positions}`\n"
        f"• Max Allocation: `{TRADING.max_allocation_pct:.0f}%` per position",
        InlineKeyboardMarkup([_back()]),
    )


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

_SETTINGS_MAP: dict[str, tuple[str, str]] = {
    "setf_qb1":   ("quick_buy_1",               "Quick Buy 1 — SOL amount (e.g. `0.1`)"),
    "setf_qb2":   ("quick_buy_2",               "Quick Buy 2 — SOL amount (e.g. `0.5`)"),
    "setf_qb3":   ("quick_buy_3",               "Quick Buy 3 — SOL amount (e.g. `1.0`)"),
    "setf_qs1":   ("quick_sell_1",              "Quick Sell 1 — percentage 1–100 (e.g. `25`)"),
    "setf_qs2":   ("quick_sell_2",              "Quick Sell 2 — percentage 1–100 (e.g. `50`)"),
    "setf_qs3":   ("quick_sell_3",              "Quick Sell 3 — percentage 1–100 (e.g. `100`)"),
    "setf_sl":    ("default_sl_pct",            "Stop Loss % (e.g. `20`). Enter `0` to clear."),
    "setf_tp":    ("default_tp_pct",            "Take Profit % (e.g. `50`). Enter `0` to clear."),
    "setf_trail": ("default_trailing_stop_pct", "Trailing Stop % (e.g. `10`). Enter `0` to disable."),
}
_NULLABLE_FIELDS = {"default_sl_pct", "default_tp_pct", "default_trailing_stop_pct"}


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    profile = await _linked_profile(_uid(update))
    if not profile:
        await _show_not_linked(update, context)
        return ConversationHandler.END
    context.user_data["profile"] = profile
    return await _show_settings_menu(update, context, profile)


async def _show_settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, profile: dict) -> int:
    s   = await db.get_bot_settings(profile["id"])
    qb1 = float(s.get("quick_buy_1")  or TRADING.quick_buy_1)
    qb2 = float(s.get("quick_buy_2")  or TRADING.quick_buy_2)
    qb3 = float(s.get("quick_buy_3")  or TRADING.quick_buy_3)
    qs1 = float(s.get("quick_sell_1") or TRADING.quick_sell_1)
    qs2 = float(s.get("quick_sell_2") or TRADING.quick_sell_2)
    qs3 = float(s.get("quick_sell_3") or TRADING.quick_sell_3)
    sl  = _risk_str(s.get("default_sl_pct"))
    tp  = _risk_str(s.get("default_tp_pct"))
    tr  = _risk_str(s.get("default_trailing_stop_pct"))

    await _show(
        update, context,
        f"⚙️ *Settings*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"*Quick Buy*  `{qb1}` · `{qb2}` · `{qb3}` SOL\n"
        f"*Quick Sell* `{qs1:.0f}%` · `{qs2:.0f}%` · `{qs3:.0f}%`\n\n"
        f"*Stop Loss:* `{sl}`  ·  *Take Profit:* `{tp}`\n"
        f"*Trailing Stop:* `{tr}`\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"_Enter 0 to clear a risk setting._",
        InlineKeyboardMarkup([
            [
                InlineKeyboardButton(f"QB1: {qb1}", callback_data="setf_qb1"),
                InlineKeyboardButton(f"QB2: {qb2}", callback_data="setf_qb2"),
                InlineKeyboardButton(f"QB3: {qb3}", callback_data="setf_qb3"),
            ],
            [
                InlineKeyboardButton(f"QS1: {qs1:.0f}%", callback_data="setf_qs1"),
                InlineKeyboardButton(f"QS2: {qs2:.0f}%", callback_data="setf_qs2"),
                InlineKeyboardButton(f"QS3: {qs3:.0f}%", callback_data="setf_qs3"),
            ],
            [
                InlineKeyboardButton(f"🛑 SL: {sl}",   callback_data="setf_sl"),
                InlineKeyboardButton(f"🎯 TP: {tp}",   callback_data="setf_tp"),
                InlineKeyboardButton(f"⚡ Trail: {tr}", callback_data="setf_trail"),
            ],
            _back(),
        ]),
    )
    return SETTINGS_VALUE


async def settings_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    key = update.callback_query.data
    if key not in _SETTINGS_MAP:
        return ConversationHandler.END
    context.user_data["settings_key"] = key
    _, description = _SETTINGS_MAP[key]
    await _show(
        update, context,
        f"⚙️ *Settings*\n\n{description}:",
        InlineKeyboardMarkup([[InlineKeyboardButton("✕ Cancel", callback_data="settings")]]),
    )
    return SETTINGS_VALUE


async def settings_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    key     = context.user_data.get("settings_key")
    mapping = _SETTINGS_MAP.get(key)
    if not mapping:
        return ConversationHandler.END
    db_field = mapping[0]
    try:
        value = float(update.message.text.strip())
        if value < 0:
            raise ValueError
    except ValueError:
        await _show(
            update, context,
            f"⚙️ *Settings*\n\n❌ Enter a valid positive number.\n\n{mapping[1]}:",
            InlineKeyboardMarkup([[InlineKeyboardButton("✕ Cancel", callback_data="settings")]]),
        )
        return SETTINGS_VALUE

    save_value = None if (db_field in _NULLABLE_FIELDS and value == 0) else value
    profile    = context.user_data.get("profile")
    if not profile:
        return ConversationHandler.END
    await db.upsert_bot_settings(profile["id"], **{db_field: save_value})
    await _show_settings_menu(update, context, profile)
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Help
# ---------------------------------------------------------------------------


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _show(
        update, context,
        "❓ *Help — FundedFrens*\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "*Trading*\n"
        "Paste any CA, ticker, or link → token loads instantly.\n"
        "Tap a buy button → executes immediately, no confirmation.\n"
        "The bot accepts ALL contract addresses — including migrated\n"
        "pump.fun tokens. If you paste a DexScreener link the CA is\n"
        "resolved automatically.\n\n"
        "*Selling*\n"
        "📊 Positions → tap a position → tap a sell button.\n"
        "Quick-sell percentages are configurable in ⚙️ Settings.\n"
        "After every sell a PnL card is sent automatically showing\n"
        "your profit/loss in SOL and USD.\n\n"
        "*Position View*\n"
        "Entry and current price are shown as *Market Cap* so you can\n"
        "see exactly where the token was and is now.\n"
        "PnL is shown in both SOL and USD.\n"
        "The full contract address is always visible and ready to copy.\n"
        "Once a position is 100% sold, sell buttons are removed.\n"
        "Tap 🔄 Refresh to update prices in-place.\n\n"
        "*Balance*\n"
        "Your start balance is locked at challenge activation and stays\n"
        "fixed — it does not change with SOL price movements.\n"
        "Buying power = start balance + realized PnL − open positions.\n\n"
        "*Risk Management*\n"
        "Configure Stop Loss, Take Profit, and Trailing Stop in ⚙️ Settings.\n"
        "Applied automatically to new positions.\n"
        "_'Not Set' means the rule is disabled for that position._\n\n"
        "*Challenge Rules*\n"
        "• Profit Target: 10%\n"
        f"• Max Drawdown: {TRADING.max_drawdown_pct:.0f}%\n"
        f"• Max Open Positions: {TRADING.max_open_positions}\n"
        f"• Max Position Size: {TRADING.max_allocation_pct:.0f}% of start balance\n"
        "• Gas fee deducted per buy and sell\n\n"
        "*Supported Inputs*\n"
        "• Solana contract address (any token, including migrated)\n"
        "• Ticker (e.g. BONK or $BONK)\n"
        "• pump.fun · DexScreener · Birdeye · Solscan · Meteora links\n"
        "━━━━━━━━━━━━━━━━━━━━",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("🌐 fundedfrens.com", url="https://fundedfrens.com")],
            _back(),
        ]),
    )


# ---------------------------------------------------------------------------
# Account PnL card (/pnl)
# ---------------------------------------------------------------------------


async def cmd_pnl(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    profile, challenge = await _profile_and_challenge(_uid(update))
    if not profile:
        await _show_not_linked(update, context)
        return
    if not challenge:
        await _show_no_challenge(update, context)
        return

    await _show(update, context, "🎴 Generating PnL card...")
    summary = await db.get_account_summary(profile["id"], challenge)
    trades  = await db.get_trades(profile["id"], challenge_id=challenge["id"], limit=1000)

    sell_trades  = [t for t in trades if t.get("side") == "sell"]
    winners      = sum(1 for t in sell_trades if float(t.get("pnl_sol") or 0) > 0)
    win_rate     = winners / len(sell_trades) * 100 if sell_trades else 0.0
    trading_days = len({t["created_at"][:10] for t in trades if t.get("created_at")})

    img = generate_pnl_card(
        username=profile.get("username") or "Trader",
        plan_name=summary["plan_name"],
        realized_pnl=summary["realized_pnl"],
        pnl_pct=summary["pnl_pct"],
        win_rate=win_rate,
        total_trades=len(trades),
        start_balance=summary["start_balance"],
        current_balance=summary["total_equity"],
        challenge_progress=float(challenge.get("challenge_progress") or 0),
        drawdown=summary["drawdown_pct"],
        trading_days=trading_days,
        sol_price=summary["sol_price"],
    )

    await context.bot.send_photo(
        chat_id=update.effective_chat.id,
        photo=img,
        caption=f"🎴 *PnL Card — {profile.get('username', 'Trader')}*",
        parse_mode="Markdown",
    )


# ---------------------------------------------------------------------------
# Buy — executes immediately, no confirmation
# ---------------------------------------------------------------------------


async def _execute_buy(update: Update, context: ContextTypes.DEFAULT_TYPE, amount: float) -> int:
    profile   = context.user_data.get("profile")
    challenge = context.user_data.get("challenge")
    token     = context.user_data.get("current_token")

    if not all([profile, challenge, token]):
        await _show(update, context, "⏱ Session expired. Paste the token again.", _home_keyboard())
        return ConversationHandler.END

    summary    = await db.get_account_summary(profile["id"], challenge)
    total_cost = amount + GAS_FEE_SOL
    if total_cost > summary["available_sol"]:
        await _show(
            update, context,
            f"❌ *Insufficient Buying Power*\n\n"
            f"Available: `{_fmt_sol(summary['available_sol'])}`\n"
            f"Requested: `{_fmt_sol(amount)}`\n"
            f"Gas fee:   `{_fmt_sol(GAS_FEE_SOL)}`\n"
            f"Total:     `{_fmt_sol(total_cost)}`",
            InlineKeyboardMarkup([[InlineKeyboardButton("← Token Page", callback_data="token_refresh")]]),
        )
        return ConversationHandler.END

    await _show(update, context, "⏳ Executing buy...")

    settings  = await db.get_bot_settings(profile["id"])
    sl_pct    = _to_float_or_none(settings.get("default_sl_pct"))
    tp_pct    = _to_float_or_none(settings.get("default_tp_pct"))
    trail_pct = _to_float_or_none(settings.get("default_trailing_stop_pct"))

    sol_price   = await db.fetch_sol_price()
    pair        = await trading.get_token_price(token["address"])
    entry_price = trading.price_in_sol(pair, sol_price) if pair else (
        token["price_usd"] / sol_price if sol_price > 0 else 0
    )
    market_data = trading._extract_market_data(pair)

    result = await trading.execute_buy(
        user_id=profile["id"],
        challenge=challenge,
        token_address=token["address"],
        token_symbol=token["symbol"],
        token_name=token["name"],
        token_logo_url=token.get("logo_url"),
        amount_sol=amount,
        entry_price_sol=entry_price,
        entry_market_cap_usd=market_data["market_cap_usd"] or token.get("market_cap") or None,
        stop_loss_pct=sl_pct,
        take_profit_pct=tp_pct,
        trailing_stop_pct=trail_pct,
        liquidity_usd=market_data["liquidity_usd"],
        volume_24h_usd=market_data["volume_24h_usd"],
        price_impact_pct=market_data["price_impact_pct"],
    )

    if result["ok"]:
        # Refresh challenge data so buying power is up-to-date
        try:
            context.user_data["challenge"] = await db.get_active_challenge(profile["id"])
        except Exception:
            pass
        entry_mc = market_data["market_cap_usd"] or token.get("market_cap") or 0
        await _show(
            update, context,
            f"✅ *Buy Executed*\n\n"
            f"*{token['symbol']}*  `{_fmt_sol(amount)}`\n"
            f"Entry MC: `{_fmt_mc(float(entry_mc)) if entry_mc else '—'}`\n"
            f"Gas fee: `{_fmt_sol(GAS_FEE_SOL)}`\n\n"
            f"SL: `{_risk_str(sl_pct)}`  |  TP: `{_risk_str(tp_pct)}`  |  Trail: `{_risk_str(trail_pct)}`",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("📊 View Positions", callback_data="positions")],
                [InlineKeyboardButton("← Token Page",      callback_data="token_refresh")],
            ]),
        )
    else:
        await _show(
            update, context,
            f"❌ *Trade Rejected*\n\n{result.get('error', 'Unknown error.')}",
            InlineKeyboardMarkup([[InlineKeyboardButton("← Token Page", callback_data="token_refresh")]]),
        )
    return ConversationHandler.END


async def buy_q1(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    p = context.user_data.get("profile") or await _linked_profile(_uid(update))
    if not p: return ConversationHandler.END
    s = await db.get_bot_settings(p["id"])
    context.user_data["profile"] = p
    return await _execute_buy(update, context, float(s.get("quick_buy_1") or TRADING.quick_buy_1))


async def buy_q2(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    p = context.user_data.get("profile") or await _linked_profile(_uid(update))
    if not p: return ConversationHandler.END
    s = await db.get_bot_settings(p["id"])
    context.user_data["profile"] = p
    return await _execute_buy(update, context, float(s.get("quick_buy_2") or TRADING.quick_buy_2))


async def buy_q3(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    p = context.user_data.get("profile") or await _linked_profile(_uid(update))
    if not p: return ConversationHandler.END
    s = await db.get_bot_settings(p["id"])
    context.user_data["profile"] = p
    return await _execute_buy(update, context, float(s.get("quick_buy_3") or TRADING.quick_buy_3))


async def buy_custom_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    token = context.user_data.get("current_token")
    if not token: return ConversationHandler.END
    await _show(
        update, context,
        f"💰 *Buy {token['symbol']}*\n\nEnter the amount in SOL:",
        InlineKeyboardMarkup([[InlineKeyboardButton("✕ Cancel", callback_data="token_refresh")]]),
    )
    return BUY_CUSTOM_AMOUNT


async def buy_custom_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        amount = float(update.message.text.strip())
        if amount <= 0:
            raise ValueError
    except ValueError:
        token = context.user_data.get("current_token", {})
        await _show(
            update, context,
            f"💰 *Buy {token.get('symbol', '')}*\n\n❌ Enter a positive number (e.g. `0.5`):",
            InlineKeyboardMarkup([[InlineKeyboardButton("✕ Cancel", callback_data="token_refresh")]]),
        )
        return BUY_CUSTOM_AMOUNT
    return await _execute_buy(update, context, amount)


# ---------------------------------------------------------------------------
# Sell — executes immediately, auto PnL card sent after
# ---------------------------------------------------------------------------


async def _execute_sell(update: Update, context: ContextTypes.DEFAULT_TYPE, pct: float) -> int:
    pos_id   = context.user_data.get("current_position_id")
    position = await db.get_position(pos_id) if pos_id else None
    if not position:
        await _show(update, context, "❌ Position not found.", InlineKeyboardMarkup([_back("positions")]))
        return ConversationHandler.END

    await _show(update, context, "⏳ Executing sell...")

    sol_price   = await db.fetch_sol_price()
    pair        = await trading.get_token_price(position["token_address"])
    current     = trading.price_in_sol(pair, sol_price) if pair else float(position["entry_price_sol"])
    market_data = trading._extract_market_data(pair)

    result = await trading.execute_sell(
        position_id=pos_id,
        exit_price_sol=current,
        sell_pct=pct,
        trigger="manual",
        exit_market_cap_usd=market_data["market_cap_usd"],
        liquidity_usd=market_data["liquidity_usd"],
        volume_24h_usd=market_data["volume_24h_usd"],
        price_impact_pct=market_data["price_impact_pct"],
    )

    pnl      = result["pnl_sol"]
    pnl_usd  = pnl * sol_price
    sign     = "+" if pnl >= 0 else ""
    emoji    = _pnl_emoji(pnl)
    type_    = "Full Close" if pct >= 99.99 else f"Partial Sell ({pct:.0f}%)"
    net_received = result["received_sol"] - GAS_FEE_SOL

    await _show(
        update, context,
        f"{emoji} *Sell Executed — {type_}*\n\n"
        f"*{result['token_symbol']}*\n"
        f"Received: `{_fmt_sol(result['received_sol'])}`\n"
        f"Gas fee:  `{_fmt_sol(GAS_FEE_SOL)}`\n"
        f"Net:      `{_fmt_sol(net_received)}`\n"
        f"PnL: `{sign}{_fmt_sol(pnl)}` (`{sign}{result['pnl_pct']:.2f}%`)  "
        f"`{sign}{_fmt_price(pnl_usd)}`",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("📊 Positions", callback_data="positions")],
            [InlineKeyboardButton("🏠 Home",      callback_data="home")],
        ]),
    )

    # Auto PnL card
    try:
        profile = await _linked_profile(_uid(update))
        if profile:
            challenge = await db.get_active_challenge(profile["id"])
            img = generate_position_card(
                username=profile.get("username") or "Trader",
                plan_name=(challenge.get("challenge_plan") or "starter").title() if challenge else "—",
                token_symbol=result["token_symbol"],
                token_name=result.get("token_name") or result["token_symbol"],
                token_address=position["token_address"],
                entry_price_usd=float(position["entry_price_sol"]) * sol_price,
                current_price_usd=current * sol_price,
                amount_sol_invested=result["invested_sol"],
                current_value_sol=result["received_sol"],
                pnl_sol=pnl,
                pnl_pct=result["pnl_pct"],
                entry_market_cap_usd=float(position.get("entry_market_cap_usd") or 0) or None,
                current_market_cap_usd=market_data["market_cap_usd"],
                liquidity_usd=market_data["liquidity_usd"],
                hold_time_str=_hold_time_str(position.get("opened_at", "")),
                sol_price=sol_price,
            )
            await context.bot.send_photo(
                chat_id=update.effective_chat.id,
                photo=img,
                caption=(
                    f"🎴 *{result['token_symbol']}*  "
                    f"{sign}{pnl:.4f} SOL  ({sign}{result['pnl_pct']:.2f}%)  "
                    f"{sign}{_fmt_price(pnl_usd)}"
                ),
                parse_mode="Markdown",
            )
    except Exception as e:
        log.warning("Auto PnL card failed: %s", e)

    return ConversationHandler.END


async def _profile_for_sell(update: Update, context: ContextTypes.DEFAULT_TYPE) -> dict | None:
    return context.user_data.get("profile") or await _linked_profile(_uid(update))


async def sell_q1(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    p = await _profile_for_sell(update, context)
    s = await db.get_bot_settings(p["id"]) if p else {}
    return await _execute_sell(update, context, float(s.get("quick_sell_1") or TRADING.quick_sell_1))


async def sell_q2(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    p = await _profile_for_sell(update, context)
    s = await db.get_bot_settings(p["id"]) if p else {}
    return await _execute_sell(update, context, float(s.get("quick_sell_2") or TRADING.quick_sell_2))


async def sell_q3(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    p = await _profile_for_sell(update, context)
    s = await db.get_bot_settings(p["id"]) if p else {}
    return await _execute_sell(update, context, float(s.get("quick_sell_3") or TRADING.quick_sell_3))


async def sell_custom_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    pos_id   = context.user_data.get("current_position_id")
    position = await db.get_position(pos_id) if pos_id else None
    symbol   = position["token_symbol"] if position else "position"
    await _show(
        update, context,
        f"📤 *Sell {symbol}*\n\nEnter the percentage to sell (1–100):",
        InlineKeyboardMarkup([[InlineKeyboardButton("✕ Cancel", callback_data=f"pos_refresh_{pos_id}")]]),
    )
    return SELL_CUSTOM_PCT


async def sell_custom_pct(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        pct = float(update.message.text.strip())
        if not (1 <= pct <= 100):
            raise ValueError
    except ValueError:
        pos_id   = context.user_data.get("current_position_id")
        position = await db.get_position(pos_id) if pos_id else None
        symbol   = position["token_symbol"] if position else ""
        await _show(
            update, context,
            f"📤 *Sell {symbol}*\n\n❌ Enter a number between 1 and 100:",
            InlineKeyboardMarkup([[InlineKeyboardButton("✕ Cancel", callback_data=f"pos_refresh_{pos_id}")]]),
        )
        return SELL_CUSTOM_PCT
    return await _execute_sell(update, context, pct)


# ---------------------------------------------------------------------------
# Token detection
# ---------------------------------------------------------------------------

_SOL_ADDR_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


def _extract_address(text: str) -> str | None:
    text = text.strip()
    for pattern in [
        r"pump\.fun/(?:coin/)?([1-9A-HJ-NP-Za-km-z]{32,44})",
        r"dexscreener\.com/solana/([1-9A-HJ-NP-Za-km-z]{32,44})",
        r"birdeye\.so/token/([1-9A-HJ-NP-Za-km-z]{32,44})",
        r"solscan\.io/token/([1-9A-HJ-NP-Za-km-z]{32,44})",
        r"geckoterminal\.com/solana/pools/([1-9A-HJ-NP-Za-km-z]{32,44})",
        # Meteora pool URLs — extract the base-token mint (first address segment)
        r"meteora\.ag/pools?/([1-9A-HJ-NP-Za-km-z]{32,44})",
        r"app\.meteora\.ag/(?:dlmm|pools?)/([1-9A-HJ-NP-Za-km-z]{32,44})",
    ]:
        m = re.search(pattern, text)
        if m:
            return m.group(1)
    clean = text.split("?")[0].strip()
    if _SOL_ADDR_RE.match(clean):
        return clean
    return None


# ---------------------------------------------------------------------------
# Text catch-all
# ---------------------------------------------------------------------------


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.message.text or "").strip()

    if text.upper().startswith("TG-"):
        await _do_link(update, context, text)
        return

    profile, challenge = await _profile_and_challenge(_uid(update))

    address = _extract_address(text)
    if address:
        if not profile:
            await _show_not_linked(update, context)
            return
        if not challenge:
            await _show_no_challenge(update, context)
            return
        await _show_token_page(update, context, address, profile, challenge)
        return

    ticker = text.lstrip("$").strip()
    if ticker and len(ticker) <= 20 and re.match(r"^[A-Za-z0-9]+$", ticker):
        if not profile:
            await _show_not_linked(update, context)
            return
        if not challenge:
            await _show_no_challenge(update, context)
            return
        pairs = await trading.search_token(ticker)
        if pairs:
            info = trading.extract_token_info(pairs[0])
            await _show_token_page(update, context, info["address"], profile, challenge)
        else:
            await _show(
                update, context,
                f"❌ No token found for `{ticker}`.\n\nTry pasting the full contract address.",
                InlineKeyboardMarkup([_back()]),
            )
        return

    if profile:
        await _show_home(update, context, profile)
    else:
        await _show_not_linked(update, context)


# ---------------------------------------------------------------------------
# Callback router
# ---------------------------------------------------------------------------


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q    = update.callback_query
    data = q.data
    await q.answer()
    uid  = q.from_user.id

    if data == "home":
        p = await _linked_profile(uid)
        await (_show_home(update, context, p) if p else _show_not_linked(update, context))

    elif data == "positions":
        await cmd_positions(update, context, sort=context.user_data.get("positions_sort", "newest"))

    elif data == "pos_sort_menu":
        await _show_sort_menu(update, context)

    elif data.startswith("pos_sort_"):
        sort = data[9:]
        context.user_data["positions_sort"] = sort
        await cmd_positions(update, context, sort=sort)

    elif data.startswith("pos_view_"):
        try:
            pos_id = int(data[9:])
            context.user_data["current_position_id"] = pos_id
            await _show_position_screen(update, context, pos_id)
        except ValueError:
            pass

    elif data.startswith("pos_refresh_"):
        # Refresh edits the current message in-place
        try:
            pos_id = int(data[12:])
            context.user_data["current_position_id"] = pos_id
            await _show_position_screen(update, context, pos_id, use_edit=True)
        except ValueError:
            pass

    elif data.startswith("pos_pnl_"):
        try:
            await _send_position_pnl_card(update, context, int(data[8:]))
        except ValueError:
            pass

    elif data == "portfolio":
        await cmd_portfolio(update, context)

    elif data == "challenge":
        await cmd_challenge(update, context)

    elif data == "settings":
        p = await _linked_profile(uid)
        if not p:
            await _show_not_linked(update, context)
            return
        context.user_data["profile"] = p
        await _show_settings_menu(update, context, p)

    elif data == "help":
        await cmd_help(update, context)

    elif data == "commands":
        await _show(
            update, context,
            "📋 *Commands*\n\nTap any command to run it:",
            _commands_keyboard(),
        )

    elif data == "cmd_pnl":
        await cmd_pnl(update, context)

    elif data == "token_refresh":
        # Refresh token page in-place
        token = context.user_data.get("current_token")
        p     = context.user_data.get("profile") or await _linked_profile(uid)
        ch    = context.user_data.get("challenge")
        if not ch and p:
            ch = await db.get_active_challenge(p["id"])
        if token and p and ch:
            await _show_token_page(update, context, token["address"], p, ch, use_edit=True)
        elif p:
            await _show_home(update, context, p)
        else:
            await _show_not_linked(update, context)

    elif data.startswith("token_"):
        address = data[6:]
        p = await _linked_profile(uid)
        if not p:
            await _show_not_linked(update, context)
            return
        ch = await db.get_active_challenge(p["id"])
        if not ch:
            await _show_no_challenge(update, context)
            return
        await _show_token_page(update, context, address, p, ch)


# ---------------------------------------------------------------------------
# Error handler
# ---------------------------------------------------------------------------


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Unhandled exception", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text("⚠️ Something went wrong. Please try again.")
        except Exception:
            pass


async def cancel_conv(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    p = await _linked_profile(_uid(update))
    await (_show_home(update, context, p) if p else _show_not_linked(update, context))
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Conversation handlers
# ---------------------------------------------------------------------------


def _buy_conv() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(buy_q1,           pattern="^buy_q1$"),
            CallbackQueryHandler(buy_q2,           pattern="^buy_q2$"),
            CallbackQueryHandler(buy_q3,           pattern="^buy_q3$"),
            CallbackQueryHandler(buy_custom_entry, pattern="^buy_custom$"),
        ],
        states={
            BUY_CUSTOM_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, buy_custom_amount),
                CallbackQueryHandler(cancel_conv, pattern="^(token_refresh|home)$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_conv),
            CommandHandler("home",   cancel_conv),
        ],
        allow_reentry=True,
    )


def _sell_conv() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(sell_q1,           pattern="^pos_sell_q1$"),
            CallbackQueryHandler(sell_q2,           pattern="^pos_sell_q2$"),
            CallbackQueryHandler(sell_q3,           pattern="^pos_sell_q3$"),
            CallbackQueryHandler(sell_custom_entry, pattern="^pos_sell_custom$"),
        ],
        states={
            SELL_CUSTOM_PCT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, sell_custom_pct),
                CallbackQueryHandler(cancel_conv, pattern=r"^(home|pos_refresh_\d+)$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_conv),
            CommandHandler("home",   cancel_conv),
        ],
        allow_reentry=True,
    )


def _settings_conv() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(settings_pick, pattern=r"^setf_")],
        states={
            SETTINGS_VALUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, settings_value),
                CallbackQueryHandler(settings_pick, pattern=r"^setf_"),
                CallbackQueryHandler(cancel_conv,   pattern="^(settings|home)$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_conv),
            CommandHandler("home",   cancel_conv),
        ],
        allow_reentry=True,
    )


# ---------------------------------------------------------------------------
# Background monitor
# ---------------------------------------------------------------------------


async def monitor_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await trading.check_all_positions(context.application)
    except Exception as e:
        log.error("Monitor job error: %s", e, exc_info=True)


# ---------------------------------------------------------------------------
# Health-check HTTP server (Render)
# ---------------------------------------------------------------------------


def _start_health_server() -> None:
    import os
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    port = int(os.environ.get("PORT", "10000"))

    class _H(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b"ok" if self.path == "/health" else b""
            code = 200 if self.path == "/health" else 404
            self.send_response(code)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_): pass

    threading.Thread(
        target=HTTPServer(("0.0.0.0", port), _H).serve_forever,
        daemon=True,
    ).start()
    log.info("Health-check server on port %d", port)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    log.info("Starting FundedFrens Trading Bot...")
    _start_health_server()

    app = Application.builder().token(config.BOT_TOKEN).build()

    # Register slash commands so "/" shows the full list in Telegram
    import asyncio as _asyncio
    _asyncio.get_event_loop().run_until_complete(
        app.bot.set_my_commands([
            ("start",     "Start the bot"),
            ("home",      "Home screen"),
            ("positions", "View open positions"),
            ("portfolio", "Portfolio & stats"),
            ("challenge", "Challenge account"),
            ("pnl",       "Generate PnL card"),
            ("settings",  "Configure quick-buy/sell & risk"),
            ("help",      "Help & instructions"),
        ])
    )

    app.add_handler(_buy_conv())
    app.add_handler(_sell_conv())
    app.add_handler(_settings_conv())

    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("home",      cmd_home))
    app.add_handler(CommandHandler("positions", lambda u, c: cmd_positions(u, c)))
    app.add_handler(CommandHandler("portfolio", cmd_portfolio))
    app.add_handler(CommandHandler("challenge", cmd_challenge))
    app.add_handler(CommandHandler("settings",  cmd_settings))
    app.add_handler(CommandHandler("help",      cmd_help))
    app.add_handler(CommandHandler("pnl",       cmd_pnl))

    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(error_handler)

    app.job_queue.run_repeating(monitor_job, interval=TRADING.monitor_interval_seconds, first=10)

    log.info("Bot polling started.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
