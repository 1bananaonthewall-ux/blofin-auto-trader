# God Bot — Cursor agent instruction manual

You are operating the **Blofin God Bot** stack: live Python scalper (`bot.py`), **position steward**, dashboard (port **5050**), and stack scripts. Read this before changing code or restarting processes.

**New clone setup:** help the user run `scripts\bootstrap_god_bot.ps1`, configure `.env` with **their** Blofin keys (never ask for secrets in chat), `BLOFIN_MODE=demo` first, then `God Bot.ps1 -Action ensure`. See `docs/GETTING_STARTED.md`.

## Your mission

1. Keep **one** `bot.py` instance (never duplicates).
2. Every open position must have **exchange TP/SL** (fast 3R ~1% stop / ~3% take, cross margin).
3. Daily mission: **maintain and exceed +10% account growth** (`mission_config.py`, `mission_brain.py`).
4. **Watch logs 12m:** read only **new** bytes from `logs/bot.log`; fix root causes; `restart-fresh` only when needed.

## Layout

| Path | Role |
|------|------|
| `God Bot.ps1` | `ensure` / `start` / `stop` / `restart` |
| `bot.py` | Main loop |
| `position_steward.py` | TP/SL, harvest |
| `scripts/stack_control.ps1` | `restart-fresh`, process control |
| `scripts/bootstrap_god_bot.ps1` | First-time install |
| `.env` | User secrets (from `.env.example`, gitignored) |
| `state/`, `logs/` | Runtime (gitignored) |

## Commands

```powershell
python scripts\stack_status.py
powershell -ExecutionPolicy Bypass -File ".\scripts\stack_control.ps1" -Action restart-fresh
powershell -ExecutionPolicy Bypass -File ".\God Bot.ps1" -Action ensure
```

Do not `git commit` unless the user asks.

## Hourly (if `.cursor/HOURLY_DUE` exists)

Read `.cursor/skills/blofin-hourly/SKILL.md`, run checklist, delete `HOURLY_DUE`, write `state/last_cursor_hourly.txt`.

## Log watch (12 min)

New bytes only; flag `ERROR`, `Traceback`, `TypeError`, `152002`, `102037`/`102038`, `margin_usdt`; ignore WS `403`.

## Safety

- No API keys in chat/commits.
- No naked positions — repair TP/SL or close.
- `BLOFIN_MODE=demo` until user confirms live.
- `DRY_RUN=true` = no orders.

## Docs

- `docs/GETTING_STARTED.md` — friend onboarding
- `ENGINE.md` — architecture
- `README.md` — overview

Replace `<PROJECT_ROOT>` with the repo root on this machine.
