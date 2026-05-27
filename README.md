# Blofin Auto Trader

Standalone Python bot for Blofin USDT perpetuals. See [COMPARISON.md](COMPARISON.md) for an honest comparison vs **BloHunter Connect** + [blohunter.ai](https://blohunter.ai).

Automated trading with a **daily equity target** (default +10%) and optional daily loss limit.

## Important risk notice

+10% **per day** is an extremely aggressive goal. Most professional funds target far less per **year**. This bot uses small per-trade risk (default 1% of equity), stop-loss / take-profit on each trade, and a daily loss circuit breaker — but **you can still lose your entire account**, especially with leverage.

Start with `DRY_RUN=true`, then use Blofin **demo** mode (`BLOFIN_MODE=demo`) before live trading.

## Setup

1. Copy credentials into `.env` (already created from your `1B Blofin API` file in OneDrive Documents).
2. Install and run:

```powershell
cd C:\Users\mknig\blofin-auto-trader
.\run.ps1
```

Or manually:

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\python bot.py
```

## Configuration (`.env`)

| Variable | Default | Meaning |
|----------|---------|---------|
| `BLOFIN_MODE` | `live` | `live` or `demo` (sandbox API) |
| `DRY_RUN` | `true` | Log orders without sending them |
| `DAILY_TARGET_PCT` | `0.10` | Daily profit milestone (logged when reached) |
| `STOP_ON_DAILY_TARGET` | `false` | If `true`, stop new trades after target is hit |
| `MAX_DAILY_LOSS_PCT` | `0.05` | Stop trading after -5% daily equity |
| `RISK_PER_TRADE_PCT` | `0.01` | Total portfolio risk budget per tick (split across slots) |
| `LEVERAGE` | `5` | Isolated leverage |
| `TRADE_UNIVERSE` | `all` | `all` = every affordable USDT perpetual; or set `SYMBOL` only |
| `MAX_POSITIONS` | `10` | Hard cap on simultaneous positions |
| `AUTO_MAX_POSITIONS` | `true` | Auto-lower slots when equity cannot support more margin |
| `MARGIN_UTILIZATION_PCT` | `0.75` | Max fraction of equity used across open positions |
| `SYMBOLS_PER_TICK` | `30` | How many markets to scan per loop (rate-limit friendly) |
| `SYMBOL` | `BTC/USDT:USDT` | Used when `TRADE_UNIVERSE` is not `all` |
| `POLL_SECONDS` | `60` | Seconds between strategy ticks |

## Portfolio mode

With `TRADE_UNIVERSE=all`, the bot:

1. Loads all live **USDT perpetual** markets from Blofin
2. Keeps only symbols your balance can afford (min contract margin vs equity)
3. Sets **max open positions** = min(cap, affordable count, margin budget) when `AUTO_MAX_POSITIONS=true`
4. Splits `RISK_PER_TRADE_PCT` across those slots (e.g. 1% total / 5 slots = 0.2% risk each)
5. Rotates through markets each tick (`SYMBOLS_PER_TICK`) while always managing open positions first

Small accounts may still only support **1–3** concurrent positions even with `MAX_POSITIONS=10`.

## Strategy

- 1-minute candles per symbol
- Long when EMA(9) > EMA(21) and RSI < 68
- Short when EMA(9) < EMA(21) and RSI > 32
- Market entries with attached stop (~0.8%) and take profit (~1.6%)

## Logs and state

- `logs/bot.log` — runtime log
- `state/daily.json` — UTC-day equity snapshot and target flags

## API key security

- `.env` is gitignored; rotate keys if this folder is shared
- Restrict API keys by IP on [Blofin API settings](https://blofin.com/account/apis)
- Never commit API secrets to git
