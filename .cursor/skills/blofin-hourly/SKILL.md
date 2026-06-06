---
name: blofin-hourly
description: >-
  Hourly Blofin scalper health check and optimizer pass. Use when
  HOURLY_MAINTENANCE_DUE appears, Cursor Automation runs, or the user asks for
  hourly optimize / leverage / throughput check.
---
# Blofin hourly maintenance (Cursor agent)

Run this checklist on the machine with API access (project root = God Bot repo clone). Use shell + read tools; do not change steward harvest logic or commit unless the user asks.

## 1. Snapshot

```powershell
cd <PROJECT_ROOT>
python scripts\hourly_maintain.py
# or inspect only:
python hourly_brain.py
```

Read `state/hourly_report.json`, `state/hourly_brain_state.json`, `state/ml_health.json`, `state/throughput_guard.json`, and `logs/bot.log` (last ~80 lines).
`scripts/hourly_maintain.py` runs **hourly_brain** (learned policy + autocode) which orchestrates throughput_guard, ml_health, optimizer autocode, cortex, and TPSL repair.
If `opens_60m` is below `target_opens_hr`, throughput_guard should already have nudged tuning — verify `THROUGHPUT GUARD` / `OPTIMIZER` lines in `logs/bot.log`.

## 1b. ML continuous training (required)

Confirm automatic ML is healthy:

- `ML_CONTINUOUS_TRAIN=true`, `SIGNAL_MODE=ml`
- `state/ml_shards/*.npz` count ≥ 3; recent `ml shard saved` in `logs/bot.log`
- `state/signal_model_meta.json`: `deployed=true`, `feedback_samples` near labelled outcomes count
- `state/ml_trainer_state.json`: `last_refit_ts` within ~2h; `last_outcome_labels` synced
- Log lines: `merging N real-feedback`, `ML refit`, `ML forward learning active`

If unhealthy, hourly_brain action `ml_health` handles it (flags + repair).

## 1c. Hourly brain / autocode (required)

- `HOURLY_BRAIN_ENABLED=true` — sklearn policy in `state/hourly_brain.joblib` learns from `state/hourly_brain_train.jsonl`
- `HOURLY_AUTOCODE_ENABLED=true` — rewrites `state/hourly_autocode.py` (LLM or template) for `decide(snapshot)` action list
- Check `state/hourly_brain_state.json` → `last_run.decided` / `last_run.applied`
- Log line: `HOURLY BRAIN | policy=...` and `HOURLY AUTOCODE -> ...`

## 2. Positions — true 50x only

Mission leverage is **50x** (`SCALP_LEVERAGE_MAX`). Per-symbol exchange caps apply (e.g. 1000RATS max **40x**).

- Close any open position where **instrument leverage < 50** OR **effective leverage < 50**, unless the symbol's exchange max is < 50 (then close if below that cap).
- Stale margin: `inst=50` but `eff=30` → close and let the bot re-enter at full margin.

Use the same logic as a one-shot close script; respect `dry_run` in `.env`.

## 3. Core book (if bot is running)

Do not start a second `bot.py`. If exactly one bot process is running, skip reconcile here (steward handles it). If bot is stopped, optionally:

```powershell
python -c "from pathlib import Path; from config import load_settings; from exchange_client import BlofinExchange; from position_registry import PositionRegistry; from autonomous_engine import create_engine; from core_brain import CoreBrain; s=load_settings(); ex=BlofinExchange(s); ex.load(); r=PositionRegistry(s.state_dir); eng=create_engine(s.state_dir); eng.bind_settings(s); eng.core.reconcile_book(ex,s,r,max_closes=2); print('book ok')"
```

## 4. Optimizer

If `OPTIMIZER_ENABLED=true`, run optimizer tick (no full bot loop):

```powershell
python -c "from pathlib import Path; from config import load_settings; from scalp_optimizer import ScalpOptimizer; from exchange_client import BlofinExchange; s=load_settings(); ex=BlofinExchange(s); o=ScalpOptimizer(s.state_dir,s); rep=o.maybe_optimize(ex.fetch_equity_usdt(), force=True); print(rep.summary if rep else 'optimizer skip')"
```

## 5. Report (required)

Append one line to `state/hourly_agent_log.jsonl` with: timestamp, equity, open count, non-50x closed, optimizer action, ml_health note, anomalies.

Tell the user in chat: equity, open positions with inst/eff lev, tph, any closes, optimizer note. Keep it under 15 lines.

## 6. Mark done

```powershell
python -c "from pathlib import Path; import time; p=Path('state/last_cursor_hourly.txt'); p.parent.mkdir(exist_ok=True); p.write_text(str(time.time()))"
```

Remove `.cursor/HOURLY_DUE` if present.

## Do not

- Force-push, amend commits, or edit `.env` secrets without asking
- Change `position_steward` harvest thresholds
- Run `LEV ROTATE` close-all unless user explicitly requests
