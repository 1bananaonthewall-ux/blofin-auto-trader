---
name: god-bot-caretaker
description: >-
  Periodic God Bot health check, auto-restart stack, fix blockers, and tune when
  needed. Use when GODBOT_CARETAKER_DUE exists, caretaker loop ticks, or user asks
  to babysit the stack.
---
# God Bot caretaker (Cursor agent)

Keep **one venv `bot.py`** and **dashboard :5050** healthy without user intervention.

## 0. Auto tick (local shell first)

```powershell
cd <PROJECT_ROOT>
python scripts\god_bot_caretaker_tick.py
```

Read `state/caretaker_tick.json`, `logs/caretaker.log` (tail), `logs/bot.log` (last ~60 lines).

If `action` was `ensure` or `restart-fresh` and stack is still not `ready`, continue below.

## 1. Stack ensure / restart

```powershell
powershell -File scripts\stack_control.ps1 -Action status
```

- **0 bots** or **dashboard down** → `ensure`
- **duplicate bots** or **frozen bot.log >3m** → `restart-fresh`
- Never leave **BlofinLiveBot** task enabled (duplicate worker)

Verify: `python scripts\stack_repair_check.py` → `"ready": true`

## 2. If `.cursor/STACK_REPAIR_DUE` exists

Read `.cursor/skills/stack-repair/SKILL.md` and complete that checklist first.

## 3. If `.cursor/HOURLY_DUE` exists

Read `.cursor/skills/blofin-hourly/SKILL.md` and run hourly maintain (optimizer, ML health, positions).

## 4. Code / log blockers

If `caretaker_tick.json` → `agent_reasons` mentions Traceback, ImportError, or repeated restart failure:

- Fix the root cause in code (minimal diff)
- `restart-fresh`
- Re-run `stack_repair_check.py`

Do **not** weaken winner/quality gates or add open-count caps.

## 5. Report

Append one JSON line to `state/caretaker_agent_log.jsonl`:

`ts`, `ready`, `equity` (from `stack_status.txt` or exchange), `action`, `fixes`, `anomalies`

Tell the user in ≤12 lines: stack ready?, bot pid, equity, opens, what you restarted/fixed.

## 6. Mark done

```powershell
python -c "from pathlib import Path; import time; p=Path('state/last_cursor_caretaker.txt'); p.parent.mkdir(exist_ok=True); p.write_text(str(time.time()))"
```

Delete `.cursor/GODBOT_CARETAKER_DUE` when `ready` and no open `agent_reasons`.

## Do not

- Commit `.env`, `state/`, `logs/`
- Start a second `bot.py` alongside ensure
- Force-push or amend without user ask
