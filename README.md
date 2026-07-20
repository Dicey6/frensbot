# FundedFrens Telegram Trading Terminal

A premium Telegram bot that integrates directly with the FundedFrens prop-trading platform.  
V1 operates in **simulation mode** — trades are tracked with live market data but no real blockchain transactions occur.

---

## Architecture

```
telegram_bot/
├── main.py         ← Bot entry point, all handlers & conversations
├── database.py     ← All Supabase read/write operations
├── trading.py      ← Market data (DexScreener) + trade simulation
├── pnl.py          ← Premium 1920×1080 PnL card generation
├── config.py       ← Environment variables & constants
├── requirements.txt
├── render.yaml     ← Render deployment config
├── .env.example
└── assets/
    └── fonts/      ← Fonts downloaded on first run
```

---

## Local Setup

### Prerequisites
- Python 3.11+
- A FundedFrens Supabase project with the bot schema applied

### 1 — Apply database migrations

Run the SQL in `migrations/001_bot_tables.sql` in your Supabase SQL editor.  
This adds the three new tables and two new columns required by the bot.

### 2 — Create a Telegram bot

1. Message [@BotFather](https://t.me/BotFather) on Telegram
2. Send `/newbot` and follow the prompts
3. Copy the API token

### 3 — Configure environment

```bash
cp .env.example .env
# Edit .env with your real values
```

### 4 — Install dependencies

```bash
pip install -r requirements.txt
```

### 5 — Run

```bash
python main.py
```

---

## Deployment on Render

1. Push this repository to GitHub
2. Go to [render.com](https://render.com) → New → Background Worker
3. Connect your GitHub repo
4. Set **Root Directory** to `telegram_bot`
5. Set **Build Command** to `pip install -r requirements.txt`
6. Set **Start Command** to `python main.py`
7. Add environment variables:
   - `TELEGRAM_BOT_TOKEN`
   - `SUPABASE_URL`
   - `SUPABASE_SERVICE_ROLE_KEY`
   - `APP_URL`

Or use the included `render.yaml` for infrastructure-as-code deployment.

---

## Account Linking

Users link their Telegram account to their FundedFrens account from the website:

1. User visits **fundedfrens.com → Account → Settings → Link Telegram**
2. Website calls `POST /v1/users/telegram/link-token` → returns an 8-char token
3. User sends `/link ABCD1234` to the bot
4. Bot validates the token (10-minute expiry), writes `telegram_id` to `profiles`

---

## Trading Rules

| Rule | Value |
|---|---|
| Max open positions | 3 |
| Max allocation per position | 30% of account balance |
| Supported DEXes | Raydium, Pump.fun, Moonshot, Meteora, Orca, Fluxbeam |
| Minimum liquidity | $1,000 |

---

## Market Data

All prices and market caps are sourced from the **DexScreener API** (free, no API key required).  
Position monitoring runs every **30 seconds** and automatically triggers SL/TP/TSL/AutoSell.

---

## PnL Cards

Cards are rendered at **1920×1080** using Pillow.  
JetBrains Mono font is downloaded automatically on first run.  
Cards show: token, entry/exit market cap, invested SOL, current value, profit %, profit SOL, hold time.

---

## Database Tables Used

| Table | Source | Purpose |
|---|---|---|
| `profiles` | Existing | User identity + Telegram link |
| `user_challenges` | Existing | Active challenge account |
| `challenge_plans` | Existing | Account size, rules |
| `positions` | **New** | Open simulated positions |
| `trades` | **New** | Individual buy/sell records |
| `bot_settings` | **New** | Per-user trading defaults |
