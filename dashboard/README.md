# God Bot Web Dashboard

BloHunter-style terminal UI for **God Bot** / blofin-auto-trader — dark pro aesthetic, full USDT perp universe, local-only (no secrets in the browser).

## Quick start

```powershell
cd C:\Users\mknig\blofin-auto-trader

# Terminal 1 — API (reads .env server-side)
python dashboard_api.py

# Terminal 2 — dev UI with hot reload (optional)
cd dashboard
npm run dev
# open http://127.0.0.1:5173
```

**Production (single port):**

```powershell
cd dashboard
npm run build
cd ..
python dashboard_api.py
# open http://127.0.0.1:5050
```

Or: `powershell -File scripts\run_dashboard.ps1 -Dev`

## Pages

| Tab | Purpose |
|-----|---------|
| **Terminal** | PnL equity curve, active setups, live/closed trades, developing setups, BTC sidebar |
| **Scanner** | All Blofin USDT perpetuals (495+) with search and sort |
| **Logs** | Live tail of `logs/bot.log` |
| **Settings** | Read-only engine knobs + start/stop/restart |
| **Copilot** | AI chat (same local LLM path as WhatsApp agent) |

## API (localhost only)

- `GET /api/status` — equity, mission, curve, bot running
- `GET /api/pnl-curve?limit=800` — equity time series, cumulative realized PnL, day/session baselines
- `GET /api/positions` — live positions + registry SL/TP
- `GET /api/signals` — ML signals from log
- `GET /api/trades/closed` — journal
- `GET /api/tickers?q=BTC&sort=change&limit=500` — full universe
- `GET /api/logs?n=100`
- `GET /api/settings` — sanitized config
- `POST /api/chat` — `{ "message": "..." }`
- `POST /api/stack/{start|stop|restart|status}`

Secrets never leave `dashboard_api.py`.
