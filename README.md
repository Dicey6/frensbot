# HoodFund Telegram Trading Bot

Simulated Solana trading terminal for the [HoodFund](https://hoodfund.online) challenge platform.

## Features

- **Instant trades** — paste a CA, ticker, or link → token loads → tap buy → executes immediately
- **Customisable quick-buy & quick-sell** — 3 configurable presets per user
- **Risk management** — per-position stop-loss and trailing stop enforced by the background monitor
- **Auto PnL card** — generated and sent automatically after every sell
- **Supabase as source of truth** — all challenge rules, capital, and stats read from and written to the website database
- **Helius + DexScreener** — Helius for token metadata, DexScreener for price/liquidity/volume

## Setup

```bash
cp .env.example .env
# edit .env

pip install -r bot/requirements.txt
python bot/main.py
```

## Database Migrations

Run in order in the Supabase SQL Editor:

| File | Description |
|------|-------------|
| `migrations/001_bot_tables.sql` | Creates bot_settings, positions, trades |
| `migrations/003_phase2_backend_sync.sql` | Analytics columns + challenge sync fields |
| `migrations/004_stable_balance.sql` | Locked challenge balance + customisable quick-sell |

## Deployment (Render)

Set all env vars from `.env.example` as Render Environment Variables.
The bot starts an HTTP health-check server on `$PORT` (default 10000).

## Architecture

```
bot/
  config.py     — env vars, constants, brand colours
  database.py   — Supabase data access layer
  trading.py    — token lookup, buy/sell execution, position monitor
  main.py       — Telegram handlers and conversation flows
  pnl.py        — Pillow-based PnL card renderer (1200×675 px)
migrations/
  001_bot_tables.sql
  003_phase2_backend_sync.sql
  004_stable_balance.sql
```
