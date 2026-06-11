# God Bot caretaker (every 15 minutes while this automation runs)

You operate **blofin-auto-trader** on **main** on the user's machine (live `.env`).

## Every run

1. Read `.cursor/skills/god-bot-caretaker/SKILL.md` and execute the full checklist.
2. Run `python scripts/god_bot_caretaker_tick.py` — it auto-restarts stack when bot/dashboard are down.
3. If `agent_reasons` in `state/caretaker_tick.json`, fix code and `restart-fresh`.
4. Clear `.cursor/GODBOT_CARETAKER_DUE` when `stack_repair_check.py` reports ready.

## Hard rules

- Never commit `.env`, `state/`, or `logs/`.
- One `bot.py` only; use `stack_control.ps1 -Action restart-fresh` for duplicates.
- Quality-first: no open-count caps; margin is the only limit.

## Output (≤12 lines)

```
CARETAKER | ready=true/false | equity=$X | opens=N | action=ensure|restart-fresh|ok | notes
```
