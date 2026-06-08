# Bob's Bots storefront setup

Public shop for backtest-ranked trading bots. Brand: **Bob's Bots**.

## Quick start

```powershell
cd blofin-auto-trader
powershell -ExecutionPolicy Bypass -File .\scripts\start_bobs_bots.ps1 -Build
```

Open **http://127.0.0.1:5070**

Dev mode (hot reload UI + API in separate windows):

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start_bobs_bots.ps1 -Dev
```

## Payments

Add to `.env` (never commit):

```ini
STOREFRONT_PORT=5070
STOREFRONT_SITE_URL=https://your-domain.com

# Card payments (Stripe Checkout)
STOREFRONT_STRIPE_SECRET_KEY=sk_live_...
STOREFRONT_STRIPE_WEBHOOK_SECRET=whsec_...

# Crypto (optional — show your addresses)
STOREFRONT_CRYPTO_BTC_ADDRESS=bc1q...
STOREFRONT_CRYPTO_ETH_ADDRESS=0x...
STOREFRONT_CRYPTO_USDT_TRC20=T...

# Concierge LLM (reuses WHATSAPP_LLM_* / LM Studio)
STOREFRONT_LLM_MAX_TOKENS=600
```

Without Stripe keys, checkout runs in **demo mode** (instant fulfill for testing).

## Features

| Area | Endpoint / path |
|------|-----------------|
| Catalog & rankings | `GET /api/catalog` |
| Bot backtest curve | `GET /api/bots/:slug` |
| **Backtest Lab** (all assets, 2y, starting pot) | `POST /api/backtest/run` |
| Asset universe + TradingView symbols | `GET /api/backtest/assets` |
| Single-asset backtest | `GET /api/backtest/symbol/:inst_id` |
| TradingView Pine script export | `GET /api/backtest/pine` |
| Checkout | `POST /api/checkout` |
| Concierge (refunds/deals) | `POST /api/concierge` |
| Refund requests | `POST /api/refunds` |

### Backtest Lab (UI: **Backtest Lab** nav)

- Runs Bob's Bots strategy simulation on **top N Blofin USDT perps** (by 24h volume)
- **Start / end date** pickers: any window from **7 to 730 days** (2 years max)
- **Starting pot**: $10 – $1,000,000 (scales equity curve and PnL)
- **Bar size**: 1H / 4H / 1D (OHLCV from Blofin public API)
- Results table per asset + **embedded TradingView chart** (`BLOFIN:BTCUSDT.P`, etc.)
- **Pine script** export — paste into TradingView Strategy Tester to verify on any symbol

Orders persist in `state/storefront/orders.json`.

## Product catalog

Edit `catalog/bots.json` — bot names, prices, backtest metrics, packages, promo codes.

## Free hosting

1. `cd storefront && npm run build`
2. Upload `storefront/dist` to Cloudflare Pages **or** serve via `storefront_api.py` on a $5 VPS
3. For API on Pages, use Cloudflare Workers proxy to your API host

See `docs/marketing/BOBS_BOTS_FREE_PROMOTION.md` for growth tactics.
