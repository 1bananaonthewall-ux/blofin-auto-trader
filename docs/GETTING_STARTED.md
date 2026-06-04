# Getting started — run your own God Bot (friend guide)

This guide is for someone who received access to the **God Bot** GitHub repo and wants the **same stack** as the owner, on **their own Blofin account**.

## Before you start

- Windows PC with admin rights to install Python
- A **Blofin** account (demo wallet is fine)
- API key with **Trade** (and read) permissions — [Blofin API](https://blofin.com/account/apis)
- **Cursor** installed — [cursor.com](https://cursor.com)

You do **not** need the owner’s API keys or `state/` folder.

## Step 1 — Clone in Cursor

1. Open **Cursor**.
2. `Ctrl+Shift+P` → **Git: Clone** (or terminal):

```powershell
git clone https://github.com/1bananaonthewall-ux/blofin-auto-trader.git
cd blofin-auto-trader
```

If the repo is **private**, the owner must add your GitHub user under **Settings → Collaborators** (see [OWNER_GITHUB.md](OWNER_GITHUB.md)).

3. **File → Open Folder** → select `blofin-auto-trader`.

## Step 2 — Ask Cursor to help setup

Open **Agent** chat and send:

```
I cloned God Bot. Read docs/GETTING_STARTED.md and AGENT_READ_ME_FIRST.md.
Walk me through: bootstrap, .env with MY Blofin API keys (I will paste keys only into .env, not chat),
BLOFIN_MODE=demo first, then God Bot.ps1 -Action ensure. Do not start a second bot.py.
```

## Step 3 — Bootstrap (terminal)

```powershell
cd blofin-auto-trader
powershell -ExecutionPolicy Bypass -File .\scripts\bootstrap_god_bot.ps1
```

This installs:

- Python venv + `requirements.txt` + `requirements-dashboard.txt`
- `.env` from `.env.example` (if you don’t have one yet)
- Dashboard build (if `npm` is on PATH)

## Step 4 — Configure `.env` (your keys only)

```powershell
notepad .env
```

**Minimum required:**

| Variable | Example | Notes |
|----------|---------|--------|
| `BLOFIN_API_KEY` | (from Blofin) | Required |
| `BLOFIN_SECRET` | (from Blofin) | Required |
| `BLOFIN_PASSPHRASE` | (from Blofin) | Required |
| `BLOFIN_MODE` | `demo` | Use demo until you trust live |
| `DRY_RUN` | `false` | `true` = log only, no orders |

Leave the rest as in `.env.example` for an **identical** God Bot profile (50x scalp 3R, cross margin, fast lethal TP/SL, optimizer, etc.).

**Never** commit `.env` or paste keys into Discord/email/chat.

### Optional: credential file import

If you keep keys in a local text file (same format as the owner’s doc), run:

```powershell
.\.venv\Scripts\python.exe scripts\sync_blofin_credentials.py
```

(Only works if that file exists on **your** PC — otherwise edit `.env` manually.)

## Step 5 — Smoke test

```powershell
.\.venv\Scripts\python.exe smoke_test.py
python scripts\stack_status.py
```

Fix any “missing API” or import errors before live trading.

## Step 6 — Start God Bot

```powershell
powershell -ExecutionPolicy Bypass -File ".\God Bot.ps1" -Action ensure
```

- Dashboard: **http://127.0.0.1:5050**
- Log window opens automatically; or `Get-Content logs\bot.log -Wait -Tail 40`

Confirm `stack_status` shows `live=True` or `mode=demo` as expected and open positions get **exchange TP/SL live**.

## Step 7 — Go live (only when ready)

In `.env`:

```ini
BLOFIN_MODE=live
DRY_RUN=false
```

Then:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\stack_control.ps1 -Action restart-fresh
```

## Daily commands

| Task | Command |
|------|---------|
| Health | `python scripts\stack_status.py` |
| Ensure running | `.\God Bot.ps1 -Action ensure` |
| Clean restart | `.\scripts\stack_control.ps1 -Action restart-fresh` |
| Stop | `.\God Bot.ps1 -Action stop` |

## Cursor agent on your machine

The repo ships `.cursor/skills/` and rules so the agent behaves like the owner’s setup:

- Hourly maintenance: `.cursor/skills/blofin-hourly/SKILL.md`
- Ops manual: `AGENT_READ_ME_FIRST.md`

Tell the agent: *“Follow AGENT_READ_ME_FIRST for log watches and restarts.”*

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `Missing BLOFIN_API_KEY` | Fill all three keys in `.env`, save, restart |
| Two bots / weird PnL | `stack_control.ps1 -Action restart-fresh` |
| Dashboard empty | `scripts\run_dashboard.ps1` or `cd dashboard; npm run build` |
| `403` on WebSocket | Normal — bot uses REST fallback |
| Import / `TypeError` after pull | `restart-fresh` after `git pull` |

## Different from the owner

- **Equity, positions, ML state** live in your local `state/` (created at runtime, not in git).
- **PnL and wins** depend on your account and market — not copied from the owner.
- **Optional GGUF** models are downloaded locally (`models/`, gitignored).

You now have the same **code and config profile**; only credentials and runtime state are yours.
