# Bob's Bots — free promotion playbook

Sell on **backtests**, not live PnL. Bob's Bots is built for zero paid ad spend.

## Positioning (copy-paste)

> **Bob's Bots** — trading bots ranked by historical backtest, not influencer screenshots. Install in 8 minutes. Card or crypto. Concierge handles refunds.

## Free channels

### 1. GitHub README badge
Add to your repo README:
```markdown
[![Bob's Bots](https://img.shields.io/badge/Shop-Bob's%20Bots-3dd6c6)](http://127.0.0.1:5070)
```

### 2. Reddit / forums (value-first)
Post the **profitability leaderboard** screenshot with methodology:
- Period, starting equity, max drawdown disclosed
- "We don't show live accounts — here's why"
- Link to rankings page
Subreddits: `r/algotrading`, `r/CryptoCurrency` (follow rules, no spam)

### 3. X / Threads threads
Thread template:
1. "I ranked 5 Blofin scalpers by backtest Sharpe — not hype"
2. Table image from `/rankings`
3. "Install = one PowerShell command"
4. "LAUNCH30 for 30% off launch week"
5. Pin concierge chat screenshot (refund authority)

### 4. YouTube Shorts / TikTok
- 60s: "Backtest return vs BTC hold" chart animation from bot detail page
- End card: "Bob's Bots — link in bio"

### 5. Discord / Telegram
- Free `#backtest-leaderboard` bot posting daily rank JSON from `/api/catalog`
- Concierge answers install questions

### 6. SEO (free hosting)
Deploy static `storefront/dist` to **Cloudflare Pages** or **GitHub Pages** (free):
- Title: `Bob's Bots | Backtest-Ranked Crypto Trading Bots`
- Meta description from `storefront/index.html`

### 7. Product Hunt (free launch)
Launch as "Bob's Bots — backtest-ranked trading bot shop"
- Hunter tip: lead with dispute/refund concierge angle (differentiator)

### 8. Indie Hackers / Hacker News Show HN
Title: **Show HN: Bob's Bots – sell trading bots on backtests with an LLM that can refund**

## Promo codes (built-in)

| Code | Discount |
|------|----------|
| LAUNCH30 | 30% any bot |
| STACK20 | 20% when buying 2+ items |

Concierge can mint custom codes (`SORRY15`, etc.) via chat.

## Metrics to track

- `/api/health` — uptime
- `state/storefront/orders.json` — conversions
- Concierge chat topics → FAQ improvements

## Deploy checklist (still $0)

1. `powershell -File scripts\start_bobs_bots.ps1 -Build`
2. Point domain CNAME to Cloudflare Pages
3. Set `.env`: `STOREFRONT_STRIPE_SECRET_KEY`, crypto addresses, `STOREFRONT_SITE_URL`
4. Share leaderboard + LAUNCH30 everywhere
