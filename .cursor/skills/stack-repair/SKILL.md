# Stack repair — bring God Bot online

Use when `.cursor/STACK_REPAIR_DUE` exists (Ctrl+F7 from dashboard) or `state/stack_repair.json` shows `status: running`.

## Goal

Get **one venv `bot.py`** running and **dashboard API on :5050** listening. Do not stop until `scripts/stack_repair_check.py` exits 0 or `state/stack_repair.json` has `"status": "done"`.

## Checklist

1. Read `state/stack_repair.json`, `logs/stack_agent_repair.log`, and `logs/bot.log` (tail).
2. Run status: `powershell -File scripts\stack_control.ps1 -Action status`
3. If duplicate bots or system-python stray: fix `scripts/stack_control.ps1` dedup/pid logic; run `ensure`.
4. If bot won't start: inspect `logs/bot.log` for import/LLM errors; fix code; run `restart-fresh`.
5. If dashboard down: `powershell -File scripts\start_dashboard_quiet.ps1 -Port 5050`
6. Verify: `python scripts\stack_repair_check.py` (must print `"ready": true`).
7. Delete `.cursor/STACK_REPAIR_DUE` when done.
8. Set `state/stack_repair.json` status to `done` if the background repair worker has not already.

## Constraints

- Keep `BlofinLiveBot` scheduled task **disabled** (duplicate worker).
- Prefer `.venv\Scripts\python.exe` for bot and dashboard.
- Do not commit secrets (`.env`).
