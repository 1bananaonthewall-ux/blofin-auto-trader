# God Bot — Cursor agent instruction manual (new computer)

You are operating the **Blofin God Bot** stack: a live Python scalper (`bot.py`), background **position steward**, optional **dashboard** (port 5050), and **stack guard** scripts. Read this entire file before changing code or restarting processes.

## Your mission

1. Keep **one** live `bot.py` instance running (never start duplicates).
2. Protect open positions: every live position must have **exchange TP/SL** (fast 3R: ~1% stop / ~3% take on cross margin).
3. Honor the daily mission: **maintain and exceed +10% account growth per day** (`mission_config.py` / `mission_brain.py`).
4. When the user says **watch logs 12m and fix errors**: poll `logs/bot.log` for new lines only, fix root causes, restart only if needed (stale code, `TypeError`, repeated TPSL verify failures on real opens).

## Project layout (after unzip)

| Path | Role |
|------|------|
| `God Bot.ps1` | Main launcher: `ensure` / `start` / `stop` / `restart` / `status` |
| `bot.py` | Trading loop (do not run a second copy) |
| `position_steward.py` | Background TP/SL, harvest, adoption |
| `exchange_client.py` | Orders, TPSL, sizing |
| `scripts/stack_control.ps1` | Process control, `restart-fresh` |
| `scripts/run_god_bot_stack.ps1` | Bot + dashboard ensure |
| `scripts/stack_status.py` | One-line health snapshot |
| `.env` | **Secrets — user creates from `.env.example` (never commit)** |
| `state/` | Runtime JSON (created at run; not in zip) |
| `logs/bot.log` | Primary log |
| `.cursor/skills/blofin-hourly/` | Hourly maintenance skill |
| `.cursor/rules/` | Workspace rules (e.g. hourly due file) |

## First-time setup on this machine (human + you)

1. Unzip to a fixed path, e.g. `C:\Users\<name>\God Bot` or `C:\Users\<name>\blofin-auto-trader`.
2. Install **Python 3.12+** and **PowerShell 5+**.
3. From project root:

```powershell
cd "C:\path\to\God Bot"
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\pip install -r requirements-dashboard.txt
copy .env.example .env
# User fills BLOFIN_API_KEY, BLOFIN_SECRET, BLOFIN_PASSPHRASE in .env
```

4. Dashboard (optional but recommended):

```powershell
cd dashboard
npm install
npm run build
cd ..
```

5. Start stack:

```powershell
powershell -ExecutionPolicy Bypass -File ".\God Bot.ps1" -Action ensure
```

6. Open `http://127.0.0.1:5050` for the dashboard.

## Commands you should use

```powershell
cd "<PROJECT_ROOT>"
python scripts\stack_status.py
powershell -ExecutionPolicy Bypass -File ".\scripts\stack_control.ps1" -Action status
powershell -ExecutionPolicy Bypass -File ".\scripts\stack_control.ps1" -Action restart-fresh
powershell -ExecutionPolicy Bypass -File ".\God Bot.ps1" -Action ensure
```

- **`restart-fresh`**: stop all bot PIDs, clear stale locks, start one bot — use after code fixes to `bot.py` main loop or `ImportError` / `TypeError` from partial deploy.
- **`ensure`**: idempotent start (stack guard + bot + dashboard).
- Do **not** `git commit` unless the user asks.

## Hourly maintenance (priority if `.cursor/HOURLY_DUE` exists)

1. Read `.cursor/skills/blofin-hourly/SKILL.md` and run the full checklist.
2. Delete `.cursor/HOURLY_DUE` when done; write `state/last_cursor_hourly.txt`.

## Log watch workflow (12 minutes)

1. Record byte offset of `logs/bot.log`.
2. Every ~75s, read only **new** bytes; grep for:
   - `ERROR`, `Traceback`, `TypeError`, `steward cycle failed`, `PermissionError`
   - `152002` (size string), `102037`/`102038` (TPSL vs mark), `margin_usdt`
   - Ignore `403 Forbidden` on WebSocket (REST fallback is OK).
3. Classify:
   - **Benign**: delisted symbol (`102115`), ghost TPSL repair after close, one-off rate limit.
   - **Fix**: code bug, naked position (no TP/SL), repeated open failures.
4. Restart only if fixes require `bot.py` main-loop reload or process is stuck with wrong code.

## Known issues and fixes

| Symptom | Likely cause | Action |
|---------|----------------|--------|
| `exchange TP/SL missing on BLESS` right after TP close | Stale position snapshot | Ensure `_count_unprotected_positions` verifies live position; refresh positions before gate |
| `open failed AVA` code `102115` | Delisted | Block symbol 7d in `try_open` on delist errors |
| `152002` Parameter size | Float size string | `_quantize_order_size` in `exchange_client.py` |
| `102037`/`102038` TPSL | Mark moved through triggers | `adjust_triggers_for_market` + retry in `_place_tpsl_leg` |
| `notify_trade_close() ... margin_usdt` | Stale bot process | `restart-fresh` |
| `PermissionError` on `account_snapshot.json.tmp` | Dashboard + bot race | `dashboard_publish._atomic_write` retries |
| Wrong closed PnL on dashboard | Outcome row vs profitability | `roe_learning.resolve_close_pnl_roe`, rebuild dashboard |

## Safety rules

- Never copy API keys into chat, commits, or this manual.
- Never run two `bot.py` processes.
- Never disable TP/SL to “fix” errors — repair or close unprotected positions.
- Prefer **demo** (`MODE=demo`) until the user confirms live.
- `DRY_RUN=true` logs only — no exchange orders.

## Architecture (short)

```
Scan universe → TA confluence + ML → conviction rank → entry pacer (1 elite per cycle)
       → open cross position → attach fast 3R TP/SL on exchange
       → steward loop: verify TP/SL, harvest, core_brain book
```

Key env flags (see `.env.example`): `SCALP_FAST_3R`, `SCALP_SKIP_LIQ_TPSL`, `HOURLY_3R_WINNER_MODE`, `STACK_WINNERS_MODE`, `MARGIN_MODE=cross`, `SCALP_LEVERAGE_MAX=50`.

## When to escalate to the user

- Missing or invalid `.env` / API auth failures.
- Equity fetch down for extended periods.
- Repeated emergency closes / naked positions after repair attempts.
- They must approve **live** mode and leverage caps.

## Documentation map

- `ENGINE.md` — architecture
- `README.md` — human setup
- `SETUP_NEW_COMPUTER.md` — step-by-step install on a new PC
- `AUTONOMOUS.md`, `ML.md` — subsystems

Replace `<PROJECT_ROOT>` in commands with the actual folder path on this machine.
