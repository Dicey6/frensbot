"""
main.py — FundedFrens Telegram Trading Bot entry point.

Bot features (trading terminal only):
  /start → Home screen (or link prompt if not linked)
  /link  → Link Telegram to a FundedFrens account
  /home  → Home screen with account summary
  /buy   → Search and buy a Solana token
  /sell  → Sell an open position
  /positions → View open positions with live PnL
  /portfolio → Recent trade history
  /settings  → View / edit default risk parameters
  /pnl  → Generate and send a PnL summary card

Field mapping vs. original bot (ALL corrected here):
  profile["auth_user_id"] → profile["id"]
  telegram_link_token     → telegram_link_code
  telegram_status TEXT    → telegram_linked BOOLEAN
  user_challenges table   → challenges table
  challenge_plans table   → PLAN_USD dict in config.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import Any

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
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
from pnl import generate_pnl_card

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Conversation states
# ---------------------------------------------------------------------------

(
    BUY_TOKEN_INPUT,
    BUY_AMOUNT_INPUT,
    BUY_CONFIRM,
    SELL_SELECT,
    SELL_PCT_INPUT,
    SELL_CONFIRM,
    SETTINGS_FIELD,
    SETTINGS_VALUE,
    LINK_CODE_INPUT,
) = range(9)

# ---------------------------------------------------------------------------
# Shared menus
# ---------------------------------------------------------------------------

MAIN_MENU_KEYBOARD = InlineKeyboardMarkup([
    [
        InlineKeyboardButton("💰 Buy",       callback_data="menu_buy"),
        InlineKeyboardButton("📤 Sell",      callback_data="menu_sell"),
    ],
    [
        InlineKeyboardButton("📊 Positions", callback_data="menu_positions"),
        InlineKeyboardButton("📁 Portfolio", callback_data="menu_portfolio"),
    ],
    [
        InlineKeyboardButton("⚙️ Settings",  callback_data="menu_settings"),
        InlineKeyboardButton("🎴 PnL Card",  callback_data="menu_pnl"),
    ],
    [
        InlineKeyboardButton("🔄 Refresh",   callback_data="menu_home"),
    ],
])


def _back_keyboard(callback: str = "menu_home") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("← Back", callback_data=callback)]])


# ---------------------------------------------------------------------------
# Auth guard
# ---------------------------------------------------------------------------

async def _get_linked_profile(telegram_id: int) -> dict | None:
    """Return the linked profile for this Telegram user, or None."""
    profile = await db.get_profile_by_telegram_id(telegram_id)
    if profile and profile.get("telegram_linked"):
        return profile
    return None


async def _require_challenge(telegram_id: int) -> tuple[dict | None, dict | None]:
    """Return (profile, challenge) or (None, None) if not linked / no active challenge."""
    profile = await _get_linked_profile(telegram_id)
    if not profile:
        return None, None
    challenge = await db.get_active_challenge(profile["id"])
    return profile, challenge


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user    = update.effective_user
    profile = await _get_linked_profile(user.id)

    if profile:
        await _show_home(update, context, profile)
    else:
        await update.message.reply_text(
            "👋 *Welcome to FundedFrens Trading Bot!*\n\n"
            "This bot is your Solana trading terminal for your funded prop challenge.\n\n"
            "To get started, link your FundedFrens account:\n\n"
            "1. Open the FundedFrens website\n"
            "2. Go to Profile → Settings\n"
            "3. Find your *Telegram Link Code* (format: `TG-XXXXXXXXXX`)\n"
            "4. Send the command: /link `<your code>`\n\n"
            "Example: `/link TG-ABC1234567`",
            parse_mode="Markdown",
        )


# ---------------------------------------------------------------------------
# /link
# ---------------------------------------------------------------------------

async def cmd_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user

    # If already linked, say so
    existing = await _get_linked_profile(user.id)
    if existing:
        await update.message.reply_text(
            "✅ Your Telegram is already linked to a FundedFrens account.\n"
            "Use /home to see your dashboard.",
        )
        return

    # Expect the code as an argument
    args = context.args
    if not args:
        await update.message.reply_text(
            "Please include your link code:\n`/link TG-XXXXXXXXXX`",
            parse_mode="Markdown",
        )
        return

    code = args[0].strip().upper()
    if not code.startswith("TG-"):
        await update.message.reply_text(
            "❌ Invalid code format. Your code should look like `TG-XXXXXXXXXX`.",
            parse_mode="Markdown",
        )
        return

    profile = await db.get_profile_by_link_code(code)
    if not profile:
        await update.message.reply_text(
            "❌ Code not found or already used.\n\n"
            "• Double-check the code on the FundedFrens website.\n"
            "• Each code can only be used once per account.",
        )
        return

    # Link the account
    tg_username = user.username  # may be None
    success = await db.link_telegram(profile["id"], user.id, tg_username)
    if not success:
        await update.message.reply_text(
            "⚠️ Something went wrong linking your account. Please try again.",
        )
        return

    await update.message.reply_text(
        f"✅ *Account linked!*\n\n"
        f"Welcome, *{profile.get('username', 'Trader')}*! 🎉\n\n"
        f"Your FundedFrens trading terminal is ready.\n"
        f"Use /home to view your dashboard.",
        parse_mode="Markdown",
    )


# ---------------------------------------------------------------------------
# Home screen
# ---------------------------------------------------------------------------

async def cmd_home(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    profile = await _get_linked_profile(user.id)
    if not profile:
        await _not_linked(update)
        return
    await _show_home(update, context, profile)


async def _show_home(update: Update, context: ContextTypes.DEFAULT_TYPE, profile: dict) -> None:
    """Render the home screen with live account summary."""
    challenge = await db.get_active_challenge(profile["id"])

    if not challenge:
        text = (
            "⚠️ *No Active Challenge*\n\n"
            "You don't have an active funded challenge.\n"
            f"Visit [{config.APP_URL}]({config.APP_URL}) to purchase one."
        )
        msg = _get_message(update)
        if msg:
            await msg.reply_text(text, parse_mode="Markdown", disable_web_page_preview=True)
        else:
            await update.callback_query.edit_message_text(text, parse_mode="Markdown")
        return

    summary = await db.get_account_summary(profile["id"], challenge)

    sign_pnl  = "+" if summary["realized_pnl"] >= 0 else ""
    pnl_emoji = "🟢" if summary["realized_pnl"] >= 0 else "🔴"
    plan      = challenge.get("challenge_plan", "—").title()

    text = (
        f"🏠 *FundedFrens Dashboard*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"👤 `{profile.get('username', 'Trader')}` • {plan} Plan\n\n"
        f"💰 *Balance:* `{summary['available_sol']:.4f} SOL`\n"
        f"📈 *Invested:* `{summary['invested_sol']:.4f} SOL`\n"
        f"🏦 *Equity:* `{summary['total_equity']:.4f} SOL`\n\n"
        f"{pnl_emoji} *Realized PnL:* `{sign_pnl}{summary['realized_pnl']:.4f} SOL` "
        f"(`{sign_pnl}{summary['pnl_pct']:.2f}%`)\n"
        f"📉 *Drawdown:* `{summary['drawdown_pct']:.2f}%`\n\n"
        f"🎯 *Challenge:* {challenge.get('challenge_progress', 0):.2f}% complete\n"
        f"📆 *Trading Days:* {challenge.get('trading_days', 0)}\n"
        f"📊 *Open Positions:* {challenge.get('open_positions', 0)}/3\n"
        f"⚡ *Win Rate:* {challenge.get('win_rate', 0):.1f}%\n\n"
        f"💲 `1 SOL = ${summary['sol_price']:,.2f}`"
    )

    msg = _get_message(update)
    if msg:
        await msg.reply_text(text, parse_mode="Markdown", reply_markup=MAIN_MENU_KEYBOARD)
    else:
        await update.callback_query.edit_message_text(
            text, parse_mode="Markdown", reply_markup=MAIN_MENU_KEYBOARD
        )


# ---------------------------------------------------------------------------
# /buy — conversation flow
# ---------------------------------------------------------------------------

async def cmd_buy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    profile, challenge = await _require_challenge(_uid(update))
    if not profile:
        await _not_linked(update)
        return ConversationHandler.END
    if not challenge:
        await _no_challenge(update)
        return ConversationHandler.END

    context.user_data["profile"]   = profile
    context.user_data["challenge"] = challenge

    msg = _get_message(update)
    text = "🔍 *Buy Token*\n\nEnter the token symbol, name, or Solana contract address:"
    if msg:
        await msg.reply_text(text, parse_mode="Markdown",
                             reply_markup=InlineKeyboardMarkup([[
                                 InlineKeyboardButton("Cancel", callback_data="cancel_conv")
                             ]]))
    else:
        await update.callback_query.edit_message_text(text, parse_mode="Markdown",
             reply_markup=InlineKeyboardMarkup([[
                 InlineKeyboardButton("Cancel", callback_data="cancel_conv")
             ]]))
    return BUY_TOKEN_INPUT


async def buy_token_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.message.text.strip()
    await update.message.reply_text(f"🔍 Searching for `{query}`...", parse_mode="Markdown")

    pairs = await trading.search_token(query)
    if not pairs:
        await update.message.reply_text(
            "❌ No Solana tokens found. Try a different symbol or paste the contract address."
        )
        return BUY_TOKEN_INPUT

    context.user_data["search_pairs"] = pairs

    rows = []
    for i, pair in enumerate(pairs):
        info = trading.extract_token_info(pair)
        mc   = f"${info['market_cap']:,.0f}" if info["market_cap"] else "N/A"
        rows.append([InlineKeyboardButton(
            f"{i+1}. {info['symbol']} | MC: {mc} | Liq: ${info['liquidity_usd']:,.0f}",
            callback_data=f"buy_pick_{i}",
        )])
    rows.append([InlineKeyboardButton("Cancel", callback_data="cancel_conv")])

    await update.message.reply_text(
        "Select a token:",
        reply_markup=InlineKeyboardMarkup(rows),
    )
    return BUY_TOKEN_INPUT


async def buy_pick_token(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    idx   = int(update.callback_query.data.split("_")[-1])
    pairs = context.user_data.get("search_pairs", [])
    if idx >= len(pairs):
        await update.callback_query.edit_message_text("Session expired. Use /buy to start again.")
        return ConversationHandler.END

    pair = pairs[idx]
    info = trading.extract_token_info(pair)
    context.user_data["selected_token"] = info

    settings  = await db.get_bot_settings(context.user_data["profile"]["id"])
    default_b = float(settings.get("default_buy_sol") or 0.1)
    sl        = float(settings.get("default_sl_pct") or 20)
    tp        = float(settings.get("default_tp_pct") or 50)

    text = (
        f"🪙 *{info['name']} ({info['symbol']})*\n"
        f"`{info['address']}`\n\n"
        f"💲 Price: `${info['price_usd']:.8f}`\n"
        f"📊 Market Cap: `${info['market_cap']:,.0f}`\n"
        f"💧 Liquidity: `${info['liquidity_usd']:,.0f}`\n"
        f"📈 24h Change: `{info['change_24h']:+.2f}%`\n\n"
        f"Default settings: SL `{sl}%` | TP `{tp}%`\n\n"
        f"How much SOL to buy? (default: `{default_b} SOL`)\n"
        f"Reply with a number, or press the button to use the default."
    )

    await update.callback_query.edit_message_text(
        text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(f"Use default ({default_b} SOL)", callback_data=f"buy_amount_{default_b}")],
            [InlineKeyboardButton("Cancel", callback_data="cancel_conv")],
        ]),
    )
    return BUY_AMOUNT_INPUT


async def buy_amount_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle typed SOL amount."""
    try:
        amount = float(update.message.text.strip())
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Enter a positive number (e.g. `0.5`).", parse_mode="Markdown")
        return BUY_AMOUNT_INPUT

    context.user_data["buy_amount"] = amount
    return await _show_buy_confirm(update, context)


async def buy_amount_default(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle 'use default' button click."""
    await update.callback_query.answer()
    amount = float(update.callback_query.data.split("_")[-1])
    context.user_data["buy_amount"] = amount
    return await _show_buy_confirm(update, context, via_callback=True)


async def _show_buy_confirm(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    via_callback: bool = False,
) -> int:
    info     = context.user_data["selected_token"]
    amount   = context.user_data["buy_amount"]
    settings = await db.get_bot_settings(context.user_data["profile"]["id"])
    sl_pct   = float(settings.get("default_sl_pct") or 20)
    tp_pct   = float(settings.get("default_tp_pct") or 50)
    auto_pct = settings.get("default_auto_sell_pct")

    context.user_data["sl_pct"]   = sl_pct
    context.user_data["tp_pct"]   = tp_pct
    context.user_data["auto_pct"] = float(auto_pct) if auto_pct else None

    text = (
        f"✅ *Confirm Buy*\n\n"
        f"Token: `{info['symbol']}` — {info['name']}\n"
        f"Amount: `{amount:.4f} SOL`\n"
        f"Price: `${info['price_usd']:.8f}`\n"
        f"Stop Loss: `{sl_pct}%`\n"
        f"Take Profit: `{tp_pct}%`\n"
        f"Auto-sell: `{f'{auto_pct}%' if auto_pct else 'off'}`\n\n"
        f"⚠️ This is a *simulated* trade on your funded demo account."
    )
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Confirm Buy", callback_data="buy_confirm"),
            InlineKeyboardButton("❌ Cancel",       callback_data="cancel_conv"),
        ],
    ])

    if via_callback:
        await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)
    else:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)

    return BUY_CONFIRM


async def buy_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    await update.callback_query.edit_message_text("⏳ Executing buy...")

    profile   = context.user_data["profile"]
    challenge = context.user_data["challenge"]
    info      = context.user_data["selected_token"]
    amount    = context.user_data["buy_amount"]
    sl_pct    = context.user_data["sl_pct"]
    tp_pct    = context.user_data["tp_pct"]
    auto_pct  = context.user_data["auto_pct"]

    # Fetch live price for the actual entry
    pair = await trading.get_token_price(info["address"])
    if pair:
        from database import _fetch_sol_price
        sol_price    = await _fetch_sol_price()
        entry_price  = trading.price_in_sol(pair, sol_price)
        market_cap   = float(pair.get("marketCap") or pair.get("fdv") or 0)
    else:
        entry_price = info["price_usd"]  # fallback
        sol_price   = 150.0
        market_cap  = info["market_cap"]

    result = await trading.execute_buy(
        user_id=profile["id"],
        challenge=challenge,
        token_address=info["address"],
        token_symbol=info["symbol"],
        token_name=info["name"],
        token_logo_url=info.get("logo_url"),
        amount_sol=amount,
        entry_price_sol=entry_price,
        entry_market_cap_usd=market_cap,
        stop_loss_pct=sl_pct,
        take_profit_pct=tp_pct,
        auto_sell_pct=auto_pct,
    )

    if result["ok"]:
        await update.callback_query.edit_message_text(
            f"✅ *Buy Executed!*\n\n"
            f"Token: `{info['symbol']}`\n"
            f"Invested: `{amount:.4f} SOL`\n"
            f"Entry Price: `${entry_price * (sol_price or 150):.8f}`\n\n"
            f"Position ID: `{result['position_id']}`\n"
            f"SL: `{sl_pct}%` | TP: `{tp_pct}%`\n\n"
            f"Use /positions to monitor your trade.",
            parse_mode="Markdown",
        )
    else:
        await update.callback_query.edit_message_text(
            f"❌ *Buy Failed*\n\n{result.get('error', 'Unknown error.')}",
            parse_mode="Markdown",
        )

    return ConversationHandler.END


# ---------------------------------------------------------------------------
# /sell — conversation flow
# ---------------------------------------------------------------------------

async def cmd_sell(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    profile, challenge = await _require_challenge(_uid(update))
    if not profile:
        await _not_linked(update)
        return ConversationHandler.END
    if not challenge:
        await _no_challenge(update)
        return ConversationHandler.END

    positions = await db.get_open_positions(profile["id"])
    if not positions:
        msg = _get_message(update)
        text = "📂 You have no open positions to sell."
        if msg:
            await msg.reply_text(text)
        else:
            await update.callback_query.edit_message_text(text)
        return ConversationHandler.END

    context.user_data["profile"]   = profile
    context.user_data["challenge"] = challenge
    context.user_data["positions"] = positions

    rows = []
    for p in positions:
        rows.append([InlineKeyboardButton(
            f"#{p['id']} {p['token_symbol']} — {float(p['amount_sol_invested']):.4f} SOL",
            callback_data=f"sell_pos_{p['id']}",
        )])
    rows.append([InlineKeyboardButton("Cancel", callback_data="cancel_conv")])

    text = "📤 *Sell Position*\n\nSelect a position to sell:"
    msg  = _get_message(update)
    if msg:
        await msg.reply_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(rows))
    else:
        await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(rows))

    return SELL_SELECT


async def sell_select_position(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    pos_id    = int(update.callback_query.data.split("_")[-1])
    positions = context.user_data.get("positions", [])
    position  = next((p for p in positions if p["id"] == pos_id), None)
    if not position:
        await update.callback_query.edit_message_text("Position not found.")
        return ConversationHandler.END

    context.user_data["sell_position"] = position

    text = (
        f"📤 *Sell: {position['token_symbol']}*\n\n"
        f"Invested: `{float(position['amount_sol_invested']):.4f} SOL`\n\n"
        f"How much would you like to sell?\n"
        f"Reply with a percentage (e.g. `50` for 50%) or `100` for all."
    )
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("25%",  callback_data="sell_pct_25"),
            InlineKeyboardButton("50%",  callback_data="sell_pct_50"),
            InlineKeyboardButton("75%",  callback_data="sell_pct_75"),
            InlineKeyboardButton("100%", callback_data="sell_pct_100"),
        ],
        [InlineKeyboardButton("Cancel", callback_data="cancel_conv")],
    ])
    await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)
    return SELL_PCT_INPUT


async def sell_pct_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    pct = float(update.callback_query.data.split("_")[-1])
    context.user_data["sell_pct"] = pct
    return await _show_sell_confirm(update, context, via_callback=True)


async def sell_pct_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        pct = float(update.message.text.strip())
        if not (1 <= pct <= 100):
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Enter a number between 1 and 100.")
        return SELL_PCT_INPUT

    context.user_data["sell_pct"] = pct
    return await _show_sell_confirm(update, context)


async def _show_sell_confirm(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    via_callback: bool = False,
) -> int:
    position = context.user_data["sell_position"]
    pct      = context.user_data["sell_pct"]

    invested  = float(position["amount_sol_invested"])
    sell_sol  = invested * pct / 100

    text = (
        f"✅ *Confirm Sell*\n\n"
        f"Token: `{position['token_symbol']}`\n"
        f"Sell: `{pct:.0f}%` → ~`{sell_sol:.4f} SOL` at current price\n\n"
        f"⚠️ This is a *simulated* trade on your funded demo account."
    )
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Confirm Sell", callback_data="sell_confirm"),
            InlineKeyboardButton("❌ Cancel",        callback_data="cancel_conv"),
        ],
    ])
    if via_callback:
        await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)
    else:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)

    return SELL_CONFIRM


async def sell_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    await update.callback_query.edit_message_text("⏳ Executing sell...")

    position = context.user_data["sell_position"]
    pct      = context.user_data["sell_pct"]

    # Fetch live exit price
    pair = await trading.get_token_price(position["token_address"])
    if pair:
        from database import _fetch_sol_price
        sol_price  = await _fetch_sol_price()
        exit_price = trading.price_in_sol(pair, sol_price)
    else:
        # Can't fetch price — use entry as fallback (0% PnL)
        exit_price = float(position["entry_price_sol"])
        sol_price  = 150.0

    result = await trading.execute_sell(
        position_id=position["id"],
        exit_price_sol=exit_price,
        sell_pct=pct,
        trigger="manual",
    )

    sign  = "+" if result["pnl_sol"] >= 0 else ""
    emoji = "🟢" if result["pnl_sol"] >= 0 else "🔴"

    await update.callback_query.edit_message_text(
        f"{emoji} *Sell Executed!*\n\n"
        f"Token: `{result['token_symbol']}`\n"
        f"Sold: `{pct:.0f}%` of position\n"
        f"Received: `{result['received_sol']:.4f} SOL`\n"
        f"PnL: `{sign}{result['pnl_sol']:.4f} SOL ({sign}{result['pnl_pct']:.2f}%)`",
        parse_mode="Markdown",
    )
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# /positions — open positions with live PnL
# ---------------------------------------------------------------------------

async def cmd_positions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    profile, challenge = await _require_challenge(_uid(update))
    if not profile:
        await _not_linked(update)
        return

    positions = await db.get_open_positions(profile["id"])

    msg  = _get_message(update)
    send = msg.reply_text if msg else update.callback_query.edit_message_text

    if not positions:
        await send(
            "📊 *Open Positions*\n\n"
            "You have no open positions.\n"
            "Use /buy to open one.",
            parse_mode="Markdown",
            reply_markup=_back_keyboard(),
        )
        return

    # Fetch live prices for all tokens in parallel
    from database import _fetch_sol_price
    sol_price = await _fetch_sol_price()

    lines = ["📊 *Open Positions*\n"]
    for p in positions:
        pair = await trading.get_token_price(p["token_address"])
        if pair:
            current = trading.price_in_sol(pair, sol_price)
        else:
            current = float(p["entry_price_sol"])

        entry   = float(p["entry_price_sol"])
        inv     = float(p["amount_sol_invested"])
        pnl_pct = (current - entry) / entry * 100 if entry > 0 else 0
        pnl_sol = inv * pnl_pct / 100
        sign    = "+" if pnl_sol >= 0 else ""
        emoji   = "🟢" if pnl_sol >= 0 else "🔴"

        sl_txt  = f"SL {p['stop_loss_pct']}%" if p.get("stop_loss_pct") else "—"
        tp_txt  = f"TP {p['take_profit_pct']}%" if p.get("take_profit_pct") else "—"

        lines.append(
            f"{emoji} *{p['token_symbol']}* (#{p['id']})\n"
            f"  Invested: `{inv:.4f} SOL`\n"
            f"  PnL: `{sign}{pnl_sol:.4f} SOL ({sign}{pnl_pct:.2f}%)`\n"
            f"  {sl_txt} | {tp_txt}\n"
        )

    await send(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=_back_keyboard(),
    )


# ---------------------------------------------------------------------------
# /portfolio — recent trade history
# ---------------------------------------------------------------------------

async def cmd_portfolio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    profile = await _get_linked_profile(_uid(update))
    if not profile:
        await _not_linked(update)
        return

    trades = await db.get_trades(profile["id"], limit=15)

    msg  = _get_message(update)
    send = msg.reply_text if msg else update.callback_query.edit_message_text

    if not trades:
        await send(
            "📁 *Portfolio*\n\nNo trades yet. Use /buy to make your first trade.",
            parse_mode="Markdown",
            reply_markup=_back_keyboard(),
        )
        return

    lines = ["📁 *Recent Trades*\n"]
    for t in trades:
        side  = "🟢 BUY" if t["side"] == "buy" else "🔴 SELL"
        date  = (t.get("created_at") or "")[:10]
        sol   = float(t.get("amount_sol") or 0)
        pnl   = t.get("pnl_sol")

        line  = f"{side} `{t['token_symbol']}` — `{sol:.4f} SOL` ({date})"
        if pnl is not None:
            pnl_f = float(pnl)
            sign  = "+" if pnl_f >= 0 else ""
            line += f"\n  PnL: `{sign}{pnl_f:.4f} SOL`"
        lines.append(line)

    await send(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=_back_keyboard(),
    )


# ---------------------------------------------------------------------------
# /settings
# ---------------------------------------------------------------------------

SETTINGS_FIELDS = {
    "buy":       ("default_buy_sol",       "Default buy amount in SOL (e.g. 0.1)"),
    "sl":        ("default_sl_pct",        "Default stop-loss % (e.g. 20)"),
    "tp":        ("default_tp_pct",        "Default take-profit % (e.g. 50)"),
    "autosell":  ("default_auto_sell_pct", "Auto-sell at % profit (0 to disable)"),
}


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    profile = await _get_linked_profile(_uid(update))
    if not profile:
        await _not_linked(update)
        return ConversationHandler.END

    settings = await db.get_bot_settings(profile["id"])
    context.user_data["profile"]  = profile
    context.user_data["settings"] = settings

    buy    = float(settings.get("default_buy_sol") or 0.1)
    sl     = float(settings.get("default_sl_pct")  or 20)
    tp     = float(settings.get("default_tp_pct")  or 50)
    auto   = settings.get("default_auto_sell_pct")
    auto_s = f"{float(auto):.0f}%" if auto else "off"

    text = (
        f"⚙️ *Trading Settings*\n\n"
        f"Buy Amount: `{buy:.4f} SOL`\n"
        f"Stop Loss: `{sl}%`\n"
        f"Take Profit: `{tp}%`\n"
        f"Auto-sell: `{auto_s}`\n\n"
        f"Tap a setting to change it:"
    )
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"Buy: {buy:.2f} SOL", callback_data="set_buy"),
            InlineKeyboardButton(f"SL: {sl}%",          callback_data="set_sl"),
        ],
        [
            InlineKeyboardButton(f"TP: {tp}%",           callback_data="set_tp"),
            InlineKeyboardButton(f"Auto-sell: {auto_s}", callback_data="set_autosell"),
        ],
        [InlineKeyboardButton("← Back", callback_data="menu_home")],
    ])

    msg = _get_message(update)
    if msg:
        await msg.reply_text(text, parse_mode="Markdown", reply_markup=kb)
    else:
        await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)

    return SETTINGS_FIELD


async def settings_pick_field(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    key = update.callback_query.data[4:]   # strip "set_"
    if key not in SETTINGS_FIELDS:
        return ConversationHandler.END

    context.user_data["settings_key"] = key
    _, description = SETTINGS_FIELDS[key]
    await update.callback_query.edit_message_text(
        f"⚙️ *Edit Setting*\n\n{description}:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("Cancel", callback_data="cancel_conv")
        ]]),
    )
    return SETTINGS_VALUE


async def settings_value_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    key          = context.user_data.get("settings_key")
    db_field, _  = SETTINGS_FIELDS.get(key, (None, None))
    if not db_field:
        return ConversationHandler.END

    raw = update.message.text.strip()
    try:
        value = float(raw)
        if value < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Enter a valid positive number.")
        return SETTINGS_VALUE

    # 0 means disable for auto_sell
    if db_field == "default_auto_sell_pct" and value == 0:
        value = None

    profile = context.user_data["profile"]
    await db.upsert_bot_settings(profile["id"], **{db_field: value})

    await update.message.reply_text(
        f"✅ Setting updated! Use /settings to view all.",
        reply_markup=_back_keyboard("menu_home"),
    )
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# /pnl — generate PnL card
# ---------------------------------------------------------------------------

async def cmd_pnl(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    profile, challenge = await _require_challenge(_uid(update))
    if not profile:
        await _not_linked(update)
        return
    if not challenge:
        await _no_challenge(update)
        return

    msg  = _get_message(update)
    send = msg.reply_text if msg else update.callback_query.edit_message_text
    await send("🎴 Generating your PnL card...")

    summary = await db.get_account_summary(profile["id"], challenge)
    trades  = await db.get_trades(profile["id"], limit=1000)

    sell_trades  = [t for t in trades if t.get("side") == "sell"]
    total_trades = len(trades)
    total_sells  = len(sell_trades)
    winners      = sum(1 for t in sell_trades if float(t.get("pnl_sol") or 0) > 0)
    win_rate     = winners / total_sells * 100 if total_sells else 0.0
    trading_days = len({t["created_at"][:10] for t in trades if t.get("created_at")})

    img_bytes = generate_pnl_card(
        username=profile.get("username", "Trader"),
        plan_name=summary["plan_name"],
        realized_pnl=summary["realized_pnl"],
        pnl_pct=summary["pnl_pct"],
        win_rate=win_rate,
        total_trades=total_trades,
        start_balance=summary["start_balance"],
        current_balance=summary["total_equity"],
        challenge_progress=float(challenge.get("challenge_progress") or 0),
        drawdown=summary["drawdown_pct"],
        trading_days=trading_days,
        sol_price=summary["sol_price"],
    )

    chat_id = update.effective_chat.id
    await context.bot.send_photo(
        chat_id=chat_id,
        photo=img_bytes,
        caption=f"🎴 *PnL Card — {profile.get('username', 'Trader')}*",
        parse_mode="Markdown",
    )


# ---------------------------------------------------------------------------
# Callback router (main menu buttons)
# ---------------------------------------------------------------------------

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q    = update.callback_query
    data = q.data
    await q.answer()

    if data == "menu_home":
        profile = await _get_linked_profile(q.from_user.id)
        if profile:
            await _show_home(update, context, profile)
        else:
            await q.edit_message_text("You are not linked. Use /link to connect your account.")
    elif data == "menu_buy":
        await cmd_buy(update, context)
    elif data == "menu_sell":
        await cmd_sell(update, context)
    elif data == "menu_positions":
        await cmd_positions(update, context)
    elif data == "menu_portfolio":
        await cmd_portfolio(update, context)
    elif data == "menu_settings":
        await cmd_settings(update, context)
    elif data == "menu_pnl":
        await cmd_pnl(update, context)
    elif data == "cancel_conv":
        await q.edit_message_text("Cancelled. Use /home to return to the dashboard.")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _uid(update: Update) -> int:
    return update.effective_user.id


def _get_message(update: Update):
    return update.message if update.message else None


async def _not_linked(update: Update) -> None:
    text = (
        "🔗 Your Telegram is not linked to a FundedFrens account.\n\n"
        "Use /link `<your code>` to connect."
    )
    msg = _get_message(update)
    if msg:
        await msg.reply_text(text, parse_mode="Markdown")
    elif update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="Markdown")


async def _no_challenge(update: Update) -> None:
    text = (
        f"⚠️ No active funded challenge found.\n"
        f"Visit {config.APP_URL} to purchase one."
    )
    msg = _get_message(update)
    if msg:
        await msg.reply_text(text)
    elif update.callback_query:
        await update.callback_query.edit_message_text(text)


# ---------------------------------------------------------------------------
# Error handler
# ---------------------------------------------------------------------------

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Unhandled exception", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text(
            "⚠️ An unexpected error occurred. Please try again."
        )


# ---------------------------------------------------------------------------
# Application setup
# ---------------------------------------------------------------------------

def _build_buy_conv() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CommandHandler("buy", cmd_buy),
            CallbackQueryHandler(cmd_buy, pattern="^menu_buy$"),
        ],
        states={
            BUY_TOKEN_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, buy_token_input),
                CallbackQueryHandler(buy_pick_token,   pattern=r"^buy_pick_\d+$"),
                CallbackQueryHandler(cancel_conv,       pattern="^cancel_conv$"),
            ],
            BUY_AMOUNT_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, buy_amount_input),
                CallbackQueryHandler(buy_amount_default, pattern=r"^buy_amount_"),
                CallbackQueryHandler(cancel_conv,         pattern="^cancel_conv$"),
            ],
            BUY_CONFIRM: [
                CallbackQueryHandler(buy_confirm, pattern="^buy_confirm$"),
                CallbackQueryHandler(cancel_conv, pattern="^cancel_conv$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_conv),
            CallbackQueryHandler(cancel_conv, pattern="^cancel_conv$"),
        ],
        allow_reentry=True,
    )


def _build_sell_conv() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CommandHandler("sell", cmd_sell),
            CallbackQueryHandler(cmd_sell, pattern="^menu_sell$"),
        ],
        states={
            SELL_SELECT: [
                CallbackQueryHandler(sell_select_position, pattern=r"^sell_pos_\d+$"),
                CallbackQueryHandler(cancel_conv,           pattern="^cancel_conv$"),
            ],
            SELL_PCT_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, sell_pct_input),
                CallbackQueryHandler(sell_pct_button,  pattern=r"^sell_pct_\d+$"),
                CallbackQueryHandler(cancel_conv,       pattern="^cancel_conv$"),
            ],
            SELL_CONFIRM: [
                CallbackQueryHandler(sell_confirm, pattern="^sell_confirm$"),
                CallbackQueryHandler(cancel_conv,  pattern="^cancel_conv$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_conv),
            CallbackQueryHandler(cancel_conv, pattern="^cancel_conv$"),
        ],
        allow_reentry=True,
    )


def _build_settings_conv() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CommandHandler("settings", cmd_settings),
            CallbackQueryHandler(cmd_settings, pattern="^menu_settings$"),
        ],
        states={
            SETTINGS_FIELD: [
                CallbackQueryHandler(settings_pick_field, pattern=r"^set_"),
                CallbackQueryHandler(cancel_conv,          pattern="^cancel_conv$"),
            ],
            SETTINGS_VALUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, settings_value_input),
                CallbackQueryHandler(cancel_conv, pattern="^cancel_conv$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_conv),
            CallbackQueryHandler(cancel_conv, pattern="^cancel_conv$"),
        ],
        allow_reentry=True,
    )


async def cancel_conv(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg = _get_message(update)
    if msg:
        await msg.reply_text("Cancelled. Use /home to return to the dashboard.")
    elif update.callback_query:
        await update.callback_query.edit_message_text("Cancelled. Use /home to return to the dashboard.")
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def _post_init(app: Application) -> None:
    """Start background position monitor after the bot initializes."""
    asyncio.create_task(trading.monitor_positions(app))
    log.info("Position monitor started.")


def main() -> None:
    log.info("Starting FundedFrens Trading Bot...")

    app = (
        Application.builder()
        .token(config.BOT_TOKEN)
        .post_init(_post_init)
        .build()
    )

    # Conversation handlers (must be registered before generic callback handler)
    app.add_handler(_build_buy_conv())
    app.add_handler(_build_sell_conv())
    app.add_handler(_build_settings_conv())

    # Simple command handlers
    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("link",      cmd_link))
    app.add_handler(CommandHandler("home",      cmd_home))
    app.add_handler(CommandHandler("positions", cmd_positions))
    app.add_handler(CommandHandler("portfolio", cmd_portfolio))
    app.add_handler(CommandHandler("pnl",       cmd_pnl))

    # Generic inline-button router (home menu buttons not covered by convs)
    app.add_handler(CallbackQueryHandler(handle_callback))

    # Error handler
    app.add_error_handler(error_handler)

    log.info("Bot polling started. Press Ctrl+C to stop.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
