---
name: blofin-5m
description: >-
  Five-minute God Bot health pass — stack, throughput, ML, vertical curve.
  Use when AGENT_5M_DUE exists, AGENT_LOOP_TICK_godbot_5m fires, or user asks
  for proactive 5m maintain / vertical curve optimization.
---
# God Bot 5-minute maintenance (Cursor agent)

Keep the account curve vertical: one bot, flowing opens, ML learning, no silent failures.
Run every ~5 minutes when the wake loop or `AGENT_5M_DUE` flag is active.

## 1. Automated snapshot (always first)

```powershell
cd <PROJECT_ROOT>
python scripts\agent_5m_maintain.py
```

Read `state/agent_5m_report.json`, `state/log_watch.json`, `state/throughput_guard.json`,
`state/ml_health.json`, `state/pnl_curve.json`, and `logs/bot.log` (last ~60 lines).

`agent_5m_maintain.py` already runs stack ensure, log_watch_optimizer, throughput_guard,
and ML health repair hooks — do not duplicate unless the report shows gaps.

## 2. Vertical curve check (required)

From `state/pnl_curve.json` and recent `account_curve` / `vert=` lines in `logs/bot.log`:

- `last_phase` should be `vertical` or `rising`; `declining` or `flat` → investigate.
- `last_verticality` trending down → check tph, skip patterns, entry pauses, optimizer action.
- If `opens_60m` < `target_opens_hr` in throughput_guard → confirm `THROUGHPUT GUARD` / `OPTIMIZER` lines;
  nudge via optimizer tick if maintain script did not act:

```powershell
python -c "from config import load_settings; from scalp_optimizer import ScalpOptimizer; from exchange_client import BlofinExchange; s=load_settings(); ex=BlofinExchange(s); rep=ScalpOptimizer(s.state_dir,s).maybe_optimize(ex.fetch_equity_usdt(), force=True); print(rep.summary if rep else 'skip')"
```

## 3. Stack integrity

- Exactly **one** `bot.py` via `scripts\stack_control.ps1 -Action ensure`.
- Dashboard on :5050; if down, `scripts\run_dashboard.ps1`.
- Duplicate bots → `restart-fresh` once, then ensure.

## 4. Log anomalies (fix root cause)

In new `logs/bot.log` tail since last 5m pass, act on:

- `ERROR`, `Traceback`, `TypeError`, `152002`, `102037`/`102038`
- `steward: N/M positions missing exchange TP/SL` → `python scripts\repair_open_tpsl.py`
- `ML feature count` mismatch → `ml_health_guard` flags (maintain script sets these)
- No `ml shard saved` / no `ML refit` in >2h while bot running → flag `state/ml_force_refit.flag`

## 5. Report (required)

Append one JSON line to `state/agent_5m_log.jsonl`:
`ts`, `equity`, `open`, `phase`, `verticality`, `opens_60m`, `actions`, `anomalies`, `agent_fixes`.

Chat summary ≤10 lines: equity, curve phase, tph, open count, anything fixed.

## 6. Mark done

```powershell
python -c "from pathlib import Path; import time; p=Path('state/last_cursor_5m.txt'); p.parent.mkdir(exist_ok=True); p.write_text(str(time.time()))"
```

Remove `.cursor/AGENT_5M_DUE` if present.

## Priority vs hourly

If `.cursor/HOURLY_DUE` also exists, run **hourly skill first** (full 50x + brain pass), then clear
`AGENT_5M_DUE` without re-running 5m unless hourly left anomalies.

## Do not

- Start a second `bot.py`
- Commit, force-push, or edit `.env` secrets without asking
- Change steward harvest thresholds
