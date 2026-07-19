"""
main.py — FundedFrens Telegram Trading Bot.

UX principles:
  - Everything runs through inline keyboards — no /command typing required mid-flow
  - Every bot message is edited in place (single active message per user)
  - User text input is deleted immediately to keep the chat clean
  - Every screen has a ← Back button
  - Plain text (including TG- link codes) is caught globally — /commands are optional shortcuts
  - Background monitor uses PTB's job_queue (not asyncio.create_task) for clean lifecycle

Commands (optional shortcuts, all work via buttons too):
  /start /home /buy /sell /positions /portfolio /settings /pnl /link
"""

from __future__ import annotations

import logging
import sys

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
from config import TRADING
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
    BUY_PICK_TOKEN,
    BUY_AMOUNT_INPUT,
    BUY_CONFIRM,
    SELL_SELECT,
    SELL_PCT_INPUT,
    SELL_CONFIRM,
    SETTINGS_PICK,
    SETTINGS_VALUE,
) = range(9)

# ---------------------------------------------------------------------------
# Keyboards
# ---------------------------------------------------------------------------

def _main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💰 Buy",        callback_data="menu_buy"),
            InlineKeyboardButton("📤 Sell",        callback_data="menu_sell"),
        ],
        [
            InlineKeyboardButton("📊 Positions",   callback_data="menu_positions"),
            InlineKeyboardButton("📁 Portfolio",   callback_data="menu_portfolio"),
        ],
        [
            InlineKeyboardButton("⚙️ Settings",    callback_data="menu_settings"),
            InlineKeyboardButton("🎴 PnL Card",    callback_data="menu_pnl"),
        ],
        [InlineKeyboardButton("🔄 Refresh",        callback_data="menu_home")],
    ])


def _back(to: str = "menu_home", label: str = "← Back") -> list[InlineKeyboardButton]:
    """Single back-button row, included on every screen."""
    return [InlineKeyboardButton(label, callback_data=to)]


def _cancel_row() -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton("✕ Cancel", callback_data="cancel_conv")]


# ---------------------------------------------------------------------------
# Core display helper — edits the active message in place
# ---------------------------------------------------------------------------

async def _show(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    keyboard: InlineKeyboardMarkup | None = None,
) -> None:
    """
    Edit the user's tracked active message if possible; otherwise send a new one
    and track it. This keeps the chat to a single bot message at a time.
    """
    chat_id = update.effective_chat.id
    msg_id  = context.user_data.get("active_message_id")

    if msg_id:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=text,
                parse_mode="Markdown",
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )
            return
        except Exception:
            pass  # Fall through: message may have been deleted or is unchanged

    sent = await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode="Markdown",
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )
    context.user_data["active_message_id"] = sent.message_id


async def _delete_user_message(update: Update) -> None:
    """Silently delete the user's inbound text message to keep the chat clean."""
    if update.message:
        try:
            await update.message.delete()
        except Exception:
            pass


def _track_callback_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """When entering a flow from a button press, track that message as active."""
    if update.callback_query:
        context.user_data["active_message_id"] = update.callback_query.message.message_id


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


def _uid(update: Update) -> int:
    return update.effective_user.id


# ---------------------------------------------------------------------------
# Home screen
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("active_message_id", None)   # always fresh on /start
    profile = await _linked_profile(_uid(update))
    if profile:
        await _show_home(update, context, profile)
    else:
        sent = await update.message.reply_text(
            "👋 *Welcome to FundedFrens!*\n\n"
            "Send your link code from the website to connect your account.\n\n"
            "You can find it at: Profile → Settings → Telegram Link Code\n\n"
            "Just paste it here — format: `TG-XXXXXXXXXX`",
            parse_mode="Markdown",
        )
        context.user_data["active_message_id"] = sent.message_id


async def cmd_home(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await _delete_user_message(update)
    profile = await _linked_profile(_uid(update))
    if not profile:
        await _show_not_linked(update, context)
        return
    await _show_home(update, context, profile)


async def _show_home(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    profile: dict,
) -> None:
    challenge = await db.get_active_challenge(profile["id"])

    if not challenge:
        await _show(
            update, context,
            "⚠️ *No Active Challenge*\n\n"
            "You don't have an active funded challenge.\n"
            f"Visit {config.APP_URL} to purchase one.",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Refresh", callback_data="menu_home")]]),
        )
        return

    summary   = await db.get_account_summary(profile["id"], challenge)
    sign      = "+" if summary["realized_pnl"] >= 0 else ""
    pnl_emoji = "🟢" if summary["realized_pnl"] >= 0 else "🔴"
    plan      = (challenge.get("challenge_plan") or "—").title()

    await _show(
        update, context,
        f"🏠 *FundedFrens Dashboard*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"👤 `{profile.get('username', 'Trader')}` • {plan} Plan\n\n"
        f"💰 *Balance:* `{summary['available_sol']:.4f} SOL`\n"
        f"📈 *Invested:* `{summary['invested_sol']:.4f} SOL`\n"
        f"🏦 *Equity:* `{summary['total_equity']:.4f} SOL`\n\n"
        f"{pnl_emoji} *Realized PnL:* `{sign}{summary['realized_pnl']:.4f} SOL "
        f"({sign}{summary['pnl_pct']:.2f}%)`\n"
        f"📉 *Drawdown:* `{summary['drawdown_pct']:.2f}%`\n\n"
        f"🎯 *Challenge:* `{challenge.get('challenge_progress', 0):.2f}%` complete\n"
        f"📆 *Trading Days:* `{challenge.get('trading_days', 0)}`\n"
        f"📊 *Open Positions:* `{challenge.get('open_positions', 0)}/3`\n"
        f"⚡ *Win Rate:* `{challenge.get('win_rate', 0):.1f}%`\n\n"
        f"💲 `1 SOL = ${summary['sol_price']:,.2f}`",
        _main_menu(),
    )


# ---------------------------------------------------------------------------
# /link — works via command OR plain text paste
# ---------------------------------------------------------------------------

async def cmd_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await _delete_user_message(update)

    existing = await _linked_profile(_uid(update))
    if existing:
        await _show(
            update, context,
            "✅ Your account is already linked.\nUse the menu to start trading.",
            _main_menu(),
        )
        return

    args = context.args or []
    if not args:
        await _show(
            update, context,
            "🔗 *Link Your Account*\n\n"
            "Paste your link code from the FundedFrens website:\n\n"
            "Profile → Settings → Telegram Link Code\n\n"
            "Format: `TG-XXXXXXXXXX`\n\n"
            "Just send the code — no command needed.",
        )
        return

    await _do_link(update, context, args[0])


async def _do_link(update: Update, context: ContextTypes.DEFAULT_TYPE, code: str) -> None:
    code = code.strip().upper()
    if not code.startswith("TG-"):
        await _show(update, context,
            "❌ That doesn't look like a link code.\n\n"
            "Format: `TG-XXXXXXXXXX`\n"
            "Find yours at: Profile → Settings → Telegram Link Code"
        )
        return

    profile = await db.get_profile_by_link_code(code)
    if not profile:
        await _show(update, context,
            "❌ *Code not found or already used.*\n\n"
            "• Double-check the code on the website\n"
            "• Each code links to one account only"
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
        f"Welcome, *{profile.get('username', 'Trader')}* 🎉\n\n"
        f"Your trading terminal is ready.",
        _main_menu(),
    )


# ---------------------------------------------------------------------------
# BUY flow
# ---------------------------------------------------------------------------

async def cmd_buy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message:
        await _delete_user_message(update)
    _track_callback_message(update, context)

    profile, challenge = await _profile_and_challenge(_uid(update))
    if not profile:
        await _show_not_linked(update, context)
        return ConversationHandler.END
    if not challenge:
        await _show_no_challenge(update, context)
        return ConversationHandler.END

    context.user_data["profile"]   = profile
    context.user_data["challenge"] = challenge

    await _show(update, context,
        "💰 *Buy Token*\n\n"
        "Step 1 of 3 — Search\n\n"
        "Enter a token symbol, name, or Solana contract address:",
        InlineKeyboardMarkup([_cancel_row()]),
    )
    return BUY_TOKEN_INPUT


async def buy_token_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.message.text.strip()
    await _delete_user_message(update)

    await _show(update, context,
        f"💰 *Buy Token*\n\n🔍 Searching for `{query}`...",
    )

    pairs = await trading.search_token(query)
    if not pairs:
        await _show(update, context,
            "💰 *Buy Token*\n\n"
            "❌ No Solana tokens found.\n\n"
            "Try a different symbol or paste the contract address:",
            InlineKeyboardMarkup([_cancel_row()]),
        )
        return BUY_TOKEN_INPUT

    context.user_data["search_pairs"] = pairs

    rows = []
    for i, pair in enumerate(pairs):
        info = trading.extract_token_info(pair)
        mc   = f"${info['market_cap']:,.0f}" if info["market_cap"] else "N/A"
        rows.append([InlineKeyboardButton(
            f"{i+1}. {info['symbol']} | MC: {mc}",
            callback_data=f"bpick_{i}",
        )])
    rows.append(_cancel_row())

    await _show(update, context,
        f"💰 *Buy Token*\n\nStep 1 of 3 — Select a token:",
        InlineKeyboardMarkup(rows),
    )
    return BUY_PICK_TOKEN


async def buy_pick_token(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    idx   = int(update.callback_query.data.split("_")[1])
    pairs = context.user_data.get("search_pairs", [])

    if idx >= len(pairs):
        await _show(update, context, "Session expired. Tap Buy to start again.", _main_menu())
        return ConversationHandler.END

    info = trading.extract_token_info(pairs[idx])
    context.user_data["selected_token"] = info

    settings   = await db.get_bot_settings(context.user_data["profile"]["id"])
    default_b  = float(settings.get("default_buy_sol") or 0.1)
    sl         = float(settings.get("default_sl_pct")  or 20)
    tp         = float(settings.get("default_tp_pct")  or 50)

    context.user_data["default_buy"] = default_b
    context.user_data["sl_pct"]      = sl
    context.user_data["tp_pct"]      = tp
    context.user_data["auto_pct"]    = settings.get("default_auto_sell_pct")

    await _show(update, context,
        f"💰 *Buy Token*\n\n"
        f"Step 2 of 3 — Amount\n\n"
        f"🪙 *{info['name']}* (`{info['symbol']}`)\n"
        f"💲 `${info['price_usd']:.8f}`\n"
        f"📊 MC: `${info['market_cap']:,.0f}` | Liq: `${info['liquidity_usd']:,.0f}`\n"
        f"📈 24h: `{info['change_24h']:+.2f}%`\n\n"
        f"SL: `{sl}%` | TP: `{tp}%` _(from your settings)_\n\n"
        f"How much SOL to buy?\n"
        f"Type an amount or tap the default:",
        InlineKeyboardMarkup([
            [InlineKeyboardButton(f"✓ Default — {default_b} SOL", callback_data=f"bamt_{default_b}")],
            _cancel_row(),
        ]),
    )
    return BUY_AMOUNT_INPUT


async def buy_amount_typed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _delete_user_message(update)
    try:
        amount = float(update.message.text.strip())
        if amount <= 0:
            raise ValueError
    except ValueError:
        info = context.user_data.get("selected_token", {})
        await _show(update, context,
            f"💰 *Buy Token* — {info.get('symbol', '')}\n\n"
            "❌ Enter a positive number (e.g. `0.5`):",
            InlineKeyboardMarkup([_cancel_row()]),
        )
        return BUY_AMOUNT_INPUT

    context.user_data["buy_amount"] = amount
    return await _show_buy_confirm(update, context)


async def buy_amount_default(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    amount = float(update.callback_query.data.split("_")[1])
    context.user_data["buy_amount"] = amount
    return await _show_buy_confirm(update, context)


async def _show_buy_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    info     = context.user_data["selected_token"]
    amount   = context.user_data["buy_amount"]
    sl_pct   = context.user_data["sl_pct"]
    tp_pct   = context.user_data["tp_pct"]
    auto_pct = context.user_data["auto_pct"]
    auto_s   = f"{float(auto_pct):.0f}%" if auto_pct else "off"

    await _show(update, context,
        f"💰 *Buy Token*\n\n"
        f"Step 3 of 3 — Confirm\n\n"
        f"🪙 `{info['symbol']}` — {info['name']}\n"
        f"💵 Amount: `{amount:.4f} SOL`\n"
        f"🛑 Stop Loss: `{sl_pct}%`\n"
        f"🎯 Take Profit: `{tp_pct}%`\n"
        f"⚡ Auto-sell: `{auto_s}`\n\n"
        f"_Simulated trade on your funded demo account._",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Confirm Buy", callback_data="buy_confirm")],
            _cancel_row(),
        ]),
    )
    return BUY_CONFIRM


async def buy_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    await _show(update, context, "⏳ Executing buy...")

    profile   = context.user_data["profile"]
    challenge = context.user_data["challenge"]
    info      = context.user_data["selected_token"]
    amount    = context.user_data["buy_amount"]
    sl_pct    = context.user_data["sl_pct"]
    tp_pct    = context.user_data["tp_pct"]
    auto_pct  = context.user_data["auto_pct"]

    pair = await trading.get_token_price(info["address"])
    if pair:
        from database import _fetch_sol_price
        sol_price   = await _fetch_sol_price()
        entry_price = trading.price_in_sol(pair, sol_price)
        market_cap  = float(pair.get("marketCap") or pair.get("fdv") or 0)
    else:
        entry_price = info["price_usd"]
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
        auto_sell_pct=float(auto_pct) if auto_pct else None,
    )

    if result["ok"]:
        await _show(update, context,
            f"✅ *Buy Executed!*\n\n"
            f"🪙 `{info['symbol']}` — Position #{result['position_id']}\n"
            f"💵 Invested: `{amount:.4f} SOL`\n"
            f"📍 Entry: `${entry_price * sol_price:.8f}`\n\n"
            f"SL: `{sl_pct}%` | TP: `{tp_pct}%`",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("📊 View Positions", callback_data="menu_positions")],
                [InlineKeyboardButton("🏠 Home",           callback_data="menu_home")],
            ]),
        )
    else:
        await _show(update, context,
            f"❌ *Buy Failed*\n\n{result.get('error', 'Unknown error.')}",
            InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Home", callback_data="menu_home")]]),
        )

    return ConversationHandler.END


# ---------------------------------------------------------------------------
# SELL flow
# ---------------------------------------------------------------------------

async def cmd_sell(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message:
        await _delete_user_message(update)
    _track_callback_message(update, context)

    profile, challenge = await _profile_and_challenge(_uid(update))
    if not profile:
        await _show_not_linked(update, context)
        return ConversationHandler.END
    if not challenge:
        await _show_no_challenge(update, context)
        return ConversationHandler.END

    positions = await db.get_open_positions(profile["id"])
    if not positions:
        await _show(update, context,
            "📤 *Sell*\n\nYou have no open positions.",
            InlineKeyboardMarkup([[InlineKeyboardButton("💰 Buy Token", callback_data="menu_buy")],
                                   _back()]),
        )
        return ConversationHandler.END

    context.user_data["profile"]   = profile
    context.user_data["challenge"] = challenge
    context.user_data["positions"] = positions

    rows = []
    for p in positions:
        rows.append([InlineKeyboardButton(
            f"#{p['id']} {p['token_symbol']} — {float(p['amount_sol_invested']):.4f} SOL",
            callback_data=f"spos_{p['id']}",
        )])
    rows.append(_back())

    await _show(update, context,
        "📤 *Sell Position*\n\nSelect which position to sell:",
        InlineKeyboardMarkup(rows),
    )
    return SELL_SELECT


async def sell_select(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    pos_id    = int(update.callback_query.data.split("_")[1])
    positions = context.user_data.get("positions", [])
    position  = next((p for p in positions if p["id"] == pos_id), None)

    if not position:
        await _show(update, context, "Position not found.", _main_menu())
        return ConversationHandler.END

    context.user_data["sell_position"] = position
    invested = float(position["amount_sol_invested"])

    await _show(update, context,
        f"📤 *Sell — {position['token_symbol']}*\n\n"
        f"Invested: `{invested:.4f} SOL`\n\n"
        f"How much to sell? Tap a button or type a percentage (1–100):",
        InlineKeyboardMarkup([
            [
                InlineKeyboardButton("25%",  callback_data="spct_25"),
                InlineKeyboardButton("50%",  callback_data="spct_50"),
                InlineKeyboardButton("75%",  callback_data="spct_75"),
                InlineKeyboardButton("100%", callback_data="spct_100"),
            ],
            _back("menu_sell"),
        ]),
    )
    return SELL_PCT_INPUT


async def sell_pct_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    pct = float(update.callback_query.data.split("_")[1])
    context.user_data["sell_pct"] = pct
    return await _show_sell_confirm(update, context)


async def sell_pct_typed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _delete_user_message(update)
    try:
        pct = float(update.message.text.strip())
        if not (1 <= pct <= 100):
            raise ValueError
    except ValueError:
        pos = context.user_data.get("sell_position", {})
        await _show(update, context,
            f"📤 *Sell — {pos.get('token_symbol', '')}*\n\n"
            "❌ Enter a number between 1 and 100:",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("25%", callback_data="spct_25"),
                 InlineKeyboardButton("50%", callback_data="spct_50"),
                 InlineKeyboardButton("75%", callback_data="spct_75"),
                 InlineKeyboardButton("100%", callback_data="spct_100")],
                _back("menu_sell"),
            ]),
        )
        return SELL_PCT_INPUT

    context.user_data["sell_pct"] = pct
    return await _show_sell_confirm(update, context)


async def _show_sell_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    position = context.user_data["sell_position"]
    pct      = context.user_data["sell_pct"]
    invested = float(position["amount_sol_invested"])

    await _show(update, context,
        f"📤 *Sell — Confirm*\n\n"
        f"Token: `{position['token_symbol']}`\n"
        f"Selling: `{pct:.0f}%` of position\n"
        f"≈ `{invested * pct / 100:.4f} SOL` at current price\n\n"
        f"_Simulated trade on your funded demo account._",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Confirm Sell", callback_data="sell_confirm")],
            _back("menu_sell"),
        ]),
    )
    return SELL_CONFIRM


async def sell_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    await _show(update, context, "⏳ Executing sell...")

    position = context.user_data["sell_position"]
    pct      = context.user_data["sell_pct"]

    pair = await trading.get_token_price(position["token_address"])
    if pair:
        from database import _fetch_sol_price
        sol_price  = await _fetch_sol_price()
        exit_price = trading.price_in_sol(pair, sol_price)
    else:
        exit_price = float(position["entry_price_sol"])

    result = await trading.execute_sell(
        position_id=position["id"],
        exit_price_sol=exit_price,
        sell_pct=pct,
        trigger="manual",
    )

    sign  = "+" if result["pnl_sol"] >= 0 else ""
    emoji = "🟢" if result["pnl_sol"] >= 0 else "🔴"

    await _show(update, context,
        f"{emoji} *Sell Executed!*\n\n"
        f"Token: `{result['token_symbol']}`\n"
        f"Sold: `{pct:.0f}%` of position\n"
        f"Received: `{result['received_sol']:.4f} SOL`\n"
        f"PnL: `{sign}{result['pnl_sol']:.4f} SOL ({sign}{result['pnl_pct']:.2f}%)`",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("📊 View Positions", callback_data="menu_positions")],
            [InlineKeyboardButton("🏠 Home",           callback_data="menu_home")],
        ]),
    )
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Positions
# ---------------------------------------------------------------------------

async def cmd_positions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await _delete_user_message(update)
    _track_callback_message(update, context)

    profile = await _linked_profile(_uid(update))
    if not profile:
        await _show_not_linked(update, context)
        return

    positions = await db.get_open_positions(profile["id"])

    if not positions:
        await _show(update, context,
            "📊 *Open Positions*\n\nNo open positions yet.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("💰 Buy Token", callback_data="menu_buy")],
                _back(),
            ]),
        )
        return

    from database import _fetch_sol_price
    sol_price = await _fetch_sol_price()

    lines = ["📊 *Open Positions*\n"]
    for p in positions:
        pair = await trading.get_token_price(p["token_address"])
        current = trading.price_in_sol(pair, sol_price) if pair else float(p["entry_price_sol"])

        entry   = float(p["entry_price_sol"])
        inv     = float(p["amount_sol_invested"])
        pnl_pct = (current - entry) / entry * 100 if entry > 0 else 0
        pnl_sol = inv * pnl_pct / 100
        sign    = "+" if pnl_sol >= 0 else ""
        emoji   = "🟢" if pnl_sol >= 0 else "🔴"
        sl_s    = f"SL {p['stop_loss_pct']}%" if p.get("stop_loss_pct") else "—"
        tp_s    = f"TP {p['take_profit_pct']}%" if p.get("take_profit_pct") else "—"

        lines.append(
            f"{emoji} *{p['token_symbol']}* (#{p['id']})\n"
            f"  `{inv:.4f} SOL` | `{sign}{pnl_sol:.4f} SOL ({sign}{pnl_pct:.2f}%)`\n"
            f"  {sl_s} | {tp_s}\n"
        )

    await _show(update, context,
        "\n".join(lines),
        InlineKeyboardMarkup([
            [InlineKeyboardButton("📤 Sell a Position", callback_data="menu_sell")],
            _back(),
        ]),
    )


# ---------------------------------------------------------------------------
# Portfolio
# ---------------------------------------------------------------------------

async def cmd_portfolio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await _delete_user_message(update)
    _track_callback_message(update, context)

    profile = await _linked_profile(_uid(update))
    if not profile:
        await _show_not_linked(update, context)
        return

    trades = await db.get_trades(profile["id"], limit=15)

    if not trades:
        await _show(update, context,
            "📁 *Portfolio*\n\nNo trades yet.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("💰 Make your first trade", callback_data="menu_buy")],
                _back(),
            ]),
        )
        return

    lines = ["📁 *Recent Trades*\n"]
    for t in trades:
        side  = "🟢 BUY" if t["side"] == "buy" else "🔴 SELL"
        date  = (t.get("created_at") or "")[:10]
        sol   = float(t.get("amount_sol") or 0)
        line  = f"{side} `{t['token_symbol']}` — `{sol:.4f} SOL` _{date}_"
        if t.get("pnl_sol") is not None:
            pnl  = float(t["pnl_sol"])
            sign = "+" if pnl >= 0 else ""
            line += f"\n  → `{sign}{pnl:.4f} SOL`"
        lines.append(line)

    await _show(update, context,
        "\n".join(lines),
        InlineKeyboardMarkup([_back()]),
    )


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

_SETTINGS_FIELDS = {
    "buy":      ("default_buy_sol",       "Default buy amount in SOL (e.g. `0.1`)"),
    "sl":       ("default_sl_pct",        "Stop-loss % (e.g. `20`)"),
    "tp":       ("default_tp_pct",        "Take-profit % (e.g. `50`)"),
    "autosell": ("default_auto_sell_pct", "Auto-sell at % profit (`0` to disable)"),
}


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message:
        await _delete_user_message(update)
    _track_callback_message(update, context)

    profile = await _linked_profile(_uid(update))
    if not profile:
        await _show_not_linked(update, context)
        return ConversationHandler.END

    context.user_data["profile"] = profile
    return await _show_settings_menu(update, context, profile)


async def _show_settings_menu(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    profile: dict,
) -> int:
    s     = await db.get_bot_settings(profile["id"])
    buy   = float(s.get("default_buy_sol") or 0.1)
    sl    = float(s.get("default_sl_pct")  or 20)
    tp    = float(s.get("default_tp_pct")  or 50)
    auto  = s.get("default_auto_sell_pct")
    auto_s = f"{float(auto):.0f}%" if auto else "off"

    await _show(update, context,
        f"⚙️ *Trading Settings*\n\n"
        f"Buy Amount: `{buy:.4f} SOL`\n"
        f"Stop Loss: `{sl}%`\n"
        f"Take Profit: `{tp}%`\n"
        f"Auto-sell: `{auto_s}`\n\n"
        f"Tap a setting to change it:",
        InlineKeyboardMarkup([
            [InlineKeyboardButton(f"💰 Buy: {buy:.2f} SOL", callback_data="setf_buy"),
             InlineKeyboardButton(f"🛑 SL: {sl}%",          callback_data="setf_sl")],
            [InlineKeyboardButton(f"🎯 TP: {tp}%",           callback_data="setf_tp"),
             InlineKeyboardButton(f"⚡ Auto: {auto_s}",      callback_data="setf_autosell")],
            _back(),
        ]),
    )
    return SETTINGS_PICK


async def settings_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    key = update.callback_query.data[5:]   # strip "setf_"
    if key not in _SETTINGS_FIELDS:
        return ConversationHandler.END

    context.user_data["settings_key"] = key
    _, description = _SETTINGS_FIELDS[key]

    await _show(update, context,
        f"⚙️ *Settings*\n\n{description}:",
        InlineKeyboardMarkup([_back("menu_settings")]),
    )
    return SETTINGS_VALUE


async def settings_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _delete_user_message(update)
    key      = context.user_data.get("settings_key")
    db_field = _SETTINGS_FIELDS.get(key, (None,))[0]
    if not db_field:
        return ConversationHandler.END

    try:
        value = float(update.message.text.strip())
        if value < 0:
            raise ValueError
    except ValueError:
        await _show(update, context,
            "⚙️ *Settings*\n\n❌ Enter a valid positive number:",
            InlineKeyboardMarkup([_back("menu_settings")]),
        )
        return SETTINGS_VALUE

    save_value = None if (db_field == "default_auto_sell_pct" and value == 0) else value
    profile    = context.user_data["profile"]
    await db.upsert_bot_settings(profile["id"], **{db_field: save_value})

    await _show_settings_menu(update, context, profile)
    return SETTINGS_PICK


# ---------------------------------------------------------------------------
# PnL card
# ---------------------------------------------------------------------------

async def cmd_pnl(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await _delete_user_message(update)
    _track_callback_message(update, context)

    profile, challenge = await _profile_and_challenge(_uid(update))
    if not profile:
        await _show_not_linked(update, context)
        return
    if not challenge:
        await _show_no_challenge(update, context)
        return

    await _show(update, context, "🎴 Generating your PnL card...")

    summary = await db.get_account_summary(profile["id"], challenge)
    trades  = await db.get_trades(profile["id"], limit=1000)

    sell_trades  = [t for t in trades if t.get("side") == "sell"]
    total_trades = len(trades)
    winners      = sum(1 for t in sell_trades if float(t.get("pnl_sol") or 0) > 0)
    win_rate     = winners / len(sell_trades) * 100 if sell_trades else 0.0
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

    await context.bot.send_photo(
        chat_id=update.effective_chat.id,
        photo=img_bytes,
        caption=f"🎴 *PnL Card — {profile.get('username', 'Trader')}*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([_back()]),
    )


# ---------------------------------------------------------------------------
# Global text catch-all
# ---------------------------------------------------------------------------

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handles any plain text sent outside a conversation.
    - TG-... codes are treated as link attempts (no /link needed)
    - Anything else shows the home dashboard or link prompt
    """
    text = update.message.text.strip()
    await _delete_user_message(update)

    if text.upper().startswith("TG-"):
        context.args = [text]
        await cmd_link(update, context)
        return

    profile = await _linked_profile(_uid(update))
    if profile:
        await _show_home(update, context, profile)
    else:
        await _show(update, context,
            "👋 Paste your FundedFrens link code to get started.\n\n"
            "Format: `TG-XXXXXXXXXX`\n\n"
            "Find it at: Profile → Settings → Telegram Link Code",
        )


# ---------------------------------------------------------------------------
# Inline button router
# ---------------------------------------------------------------------------

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q    = update.callback_query
    data = q.data
    await q.answer()

    _track_callback_message(update, context)

    if data == "menu_home":
        profile = await _linked_profile(q.from_user.id)
        if profile:
            await _show_home(update, context, profile)
        else:
            await _show(update, context,
                "You're not linked yet. Paste your `TG-XXXXXXXXXX` code to get started."
            )
    elif data == "menu_positions":
        await cmd_positions(update, context)
    elif data == "menu_portfolio":
        await cmd_portfolio(update, context)
    elif data == "menu_pnl":
        await cmd_pnl(update, context)
    elif data == "cancel_conv":
        profile = await _linked_profile(q.from_user.id)
        if profile:
            await _show_home(update, context, profile)
        else:
            await _show(update, context,
                "Paste your `TG-XXXXXXXXXX` link code to get started."
            )
    elif data == "help_link":
        await _show(update, context,
            "🔗 *How to link your account*\n\n"
            "1. Open fundedfrens.com\n"
            "2. Sign in and go to Profile\n"
            "3. Find your Telegram Link Code\n"
            "4. Copy it and paste it here\n\n"
            "Format: `TG-XXXXXXXXXX`"
        )


# ---------------------------------------------------------------------------
# Error handler
# ---------------------------------------------------------------------------

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Unhandled exception", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "⚠️ Something went wrong. Please try again."
            )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

async def _show_not_linked(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _show(update, context,
        "🔗 Account not linked.\n\nPaste your `TG-XXXXXXXXXX` code from fundedfrens.com to connect."
    )


async def _show_no_challenge(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _show(update, context,
        f"⚠️ No active challenge.\n\nVisit {config.APP_URL} to purchase one.",
        InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Refresh", callback_data="menu_home")]]),
    )


async def cancel_conv(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message:
        await _delete_user_message(update)
    profile = await _linked_profile(_uid(update))
    if profile:
        await _show_home(update, context, profile)
    else:
        await _show(update, context, "Paste your link code to get started.")
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Background monitor job (uses job_queue — not asyncio.create_task)
# ---------------------------------------------------------------------------

async def monitor_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Periodic position monitor. Registered with app.job_queue.run_repeating()
    so PTB manages its lifecycle cleanly on startup and shutdown — this prevents
    the 'Task was destroyed but it is pending!' error on Render restarts.
    """
    try:
        await trading.check_all_positions(context.application)
    except Exception as e:
        log.error("Monitor job error: %s", e, exc_info=True)


# ---------------------------------------------------------------------------
# Conversation handlers
# ---------------------------------------------------------------------------

def _buy_conv() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CommandHandler("buy",  cmd_buy),
            CallbackQueryHandler(cmd_buy, pattern="^menu_buy$"),
        ],
        states={
            BUY_TOKEN_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, buy_token_input),
                CallbackQueryHandler(cancel_conv, pattern="^cancel_conv$"),
            ],
            BUY_PICK_TOKEN: [
                CallbackQueryHandler(buy_pick_token,   pattern=r"^bpick_\d+$"),
                CallbackQueryHandler(cancel_conv,       pattern="^cancel_conv$"),
            ],
            BUY_AMOUNT_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, buy_amount_typed),
                CallbackQueryHandler(buy_amount_default, pattern=r"^bamt_"),
                CallbackQueryHandler(cancel_conv,         pattern="^cancel_conv$"),
            ],
            BUY_CONFIRM: [
                CallbackQueryHandler(buy_confirm, pattern="^buy_confirm$"),
                CallbackQueryHandler(cancel_conv, pattern="^cancel_conv$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_conv),
            CommandHandler("home",   cancel_conv),
            CallbackQueryHandler(cancel_conv, pattern="^cancel_conv$"),
            CallbackQueryHandler(cancel_conv, pattern="^menu_home$"),
        ],
        allow_reentry=True,
    )


def _sell_conv() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CommandHandler("sell", cmd_sell),
            CallbackQueryHandler(cmd_sell, pattern="^menu_sell$"),
        ],
        states={
            SELL_SELECT: [
                CallbackQueryHandler(sell_select, pattern=r"^spos_\d+$"),
                CallbackQueryHandler(cancel_conv, pattern="^(cancel_conv|menu_home)$"),
            ],
            SELL_PCT_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, sell_pct_typed),
                CallbackQueryHandler(sell_pct_button,  pattern=r"^spct_\d+$"),
                CallbackQueryHandler(cancel_conv,       pattern="^(cancel_conv|menu_sell|menu_home)$"),
            ],
            SELL_CONFIRM: [
                CallbackQueryHandler(sell_confirm, pattern="^sell_confirm$"),
                CallbackQueryHandler(cancel_conv,  pattern="^(cancel_conv|menu_sell|menu_home)$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_conv),
            CommandHandler("home",   cancel_conv),
            CallbackQueryHandler(cancel_conv, pattern="^(cancel_conv|menu_home)$"),
        ],
        allow_reentry=True,
    )


def _settings_conv() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CommandHandler("settings", cmd_settings),
            CallbackQueryHandler(cmd_settings, pattern="^menu_settings$"),
        ],
        states={
            SETTINGS_PICK: [
                CallbackQueryHandler(settings_pick, pattern=r"^setf_"),
                CallbackQueryHandler(cancel_conv,    pattern="^(cancel_conv|menu_home)$"),
            ],
            SETTINGS_VALUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, settings_value),
                CallbackQueryHandler(cancel_conv, pattern="^(cancel_conv|menu_settings|menu_home)$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_conv),
            CommandHandler("home",   cancel_conv),
            CallbackQueryHandler(cancel_conv, pattern="^(cancel_conv|menu_home)$"),
        ],
        allow_reentry=True,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    log.info("Starting FundedFrens Trading Bot...")

    app = Application.builder().token(config.BOT_TOKEN).build()

    # Conversation handlers first (higher priority than generic handlers)
    app.add_handler(_buy_conv())
    app.add_handler(_sell_conv())
    app.add_handler(_settings_conv())

    # Simple command handlers
    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("link",      cmd_link))
    app.add_handler(CommandHandler("home",      cmd_home))
    app.add_handler(CommandHandler("positions", cmd_positions))
    app.add_handler(CommandHandler("portfolio", cmd_portfolio))
    app.add_handler(CommandHandler("pnl",       cmd_pnl))

    # Generic button router (for menu buttons not handled by conversations)
    app.add_handler(CallbackQueryHandler(handle_callback))

    # Global text catch-all (lowest priority — only fires outside conversations)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # Error handler
    app.add_error_handler(error_handler)

    # Background position monitor via job_queue (clean lifecycle, no asyncio.create_task)
    app.job_queue.run_repeating(
        monitor_job,
        interval=TRADING.monitor_interval_seconds,
        first=10,
    )

    log.info("Bot polling started.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
