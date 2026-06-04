# God Bot

Holistic **Blofin USDT perpetual scalper** for Windows: fast 3R cross-margin TP/SL (~1% stop / ~3% take), position steward, ML + fluid manifold, live dashboard, and **Cursor agent** skills so another machine can run the same stack with **its own** API keys.

**Mission:** maintain and exceed **+10% account growth per day** (aggressive — read the risk section).

| Doc | Audience |
|-----|----------|
| [docs/GETTING_STARTED.md](docs/GETTING_STARTED.md) | Your friend — clone in Cursor, API keys, first run |
| [AGENT_READ_ME_FIRST.md](AGENT_READ_ME_FIRST.md) | Cursor agent on any PC |
| [ENGINE.md](ENGINE.md) | Architecture |
| [docs/OWNER_GITHUB.md](docs/OWNER_GITHUB.md) | You — publish repo & invite collaborator |

## Quick start (new machine)

**Requirements:** Windows 10/11, [Python 3.12+](https://www.python.org/downloads/), PowerShell 5+. Optional: [Node 20+](https://nodejs.org/) (rebuild dashboard), [Cursor](https://cursor.com).

### 1. Get the repo (Cursor)

1. Install **Cursor** and sign in.
2. **File → Clone repo** (or terminal):

```powershell
git clone https://github.com/YOUR_ORG/blofin-auto-trader.git
cd blofin-auto-trader
```

Replace `YOUR_ORG` with the GitHub user/org that owns the repo (see [docs/OWNER_GITHUB.md](docs/OWNER_GITHUB.md) to publish and share access).

3. **File → Open Folder** → select the cloned folder.

4. In Cursor chat, paste:

> Read `docs/GETTING_STARTED.md` and `AGENT_READ_ME_FIRST.md`. Help me finish setup: create `.env` from `.env.example`, run `scripts\bootstrap_god_bot.ps1`, then start God Bot in **demo** with my Blofin API keys.

### 2. One-shot bootstrap

```powershell
cd blofin-auto-trader
powershell -ExecutionPolicy Bypass -File .\scripts\bootstrap_god_bot.ps1
```

Creates `.venv`, installs Python deps, copies `.env.example` → `.env` if missing, builds dashboard when Node is installed.

### 3. Your Blofin API (never commit)

Edit `.env` — set **your** keys only:

```ini
BLOFIN_API_KEY=...
BLOFIN_SECRET=...
BLOFIN_PASSPHRASE=...
BLOFIN_MODE=demo
DRY_RUN=false
```

Create keys at [Blofin API management](https://blofin.com/account/apis) with **Trade** permission; restrict by IP if possible.

Stay on **`BLOFIN_MODE=demo`** until you intentionally switch to `live`.

### 4. Run God Bot

```powershell
powershell -ExecutionPolicy Bypass -File ".\God Bot.ps1" -Action ensure
```

| Service | URL |
|---------|-----|
| Dashboard | http://127.0.0.1:5050 |
| Status | `python scripts\stack_status.py` |
| Logs | `logs\bot.log` |

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\stack_control.ps1 -Action restart-fresh
powershell -ExecutionPolicy Bypass -File ".\God Bot.ps1" -Action stop
```

## What’s in the repo

- **`God Bot.ps1`** — start / stop / ensure stack (bot + dashboard)
- **`bot.py`** — main trading loop (single instance only)
- **`position_steward.py`** — exchange TP/SL verify, harvest, adoption
- **`scripts/`** — stack control, hourly health, bootstrap, audits
- **`dashboard/`** — React UI (`dist/` prebuilt; `npm run build` to rebuild)
- **`.cursor/`** — agent skills, hourly automation prompts, rules
- **`playbooks/`**, **`ml/`** — models and tuning (runtime artifacts go to `state/`, gitignored)

## Identical stack to the owner

Default `.env.example` matches the owner’s **God Bot profile** (50x cap, fast 3R, cross margin, throughput brain, optimizer, etc.). Each trader uses **their own** `.env` and gets a fresh `state/` on first run — no shared positions or secrets.

Optional local LLM (~3.6 GB): `scripts\setup_local_llm.ps1 -DownloadModel 7b` (not required for trading).

## Risk

Leveraged crypto futures can **wipe the account**. +10%/day is a mission target, not a promise. Use demo first, size small, and never share `.env`.

## License

Use at your own risk. No warranty. See [COMPARISON.md](COMPARISON.md) for context vs other tools.
