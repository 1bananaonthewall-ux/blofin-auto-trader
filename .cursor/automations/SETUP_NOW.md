# Option A — enable in 3 minutes

Repo is now **git initialized** locally. Finish Option A (Cursor cloud hourly):

## 1. Push to GitHub (private)

```powershell
cd C:\Users\mknig\blofin-auto-trader
# Create empty private repo "blofin-auto-trader" on github.com, then:
git remote add origin https://github.com/YOUR_USER/blofin-auto-trader.git
git branch -M main
git push -u origin main
```

## 2. Connect Cursor

- Cursor Settings → **GitHub** → authorize
- [cursor.com/automations](https://cursor.com/automations) → **New automation**

## 3. Automation settings

| Field | Value |
|--------|--------|
| Trigger | Schedule → **Every hour** |
| Repository | `blofin-auto-trader` / `main` |
| Prompt | Paste from `blofin-hourly-optimize.md` (below `---`) |

## 4. What cloud can vs cannot do

| Works in cloud | Needs your PC |
|----------------|---------------|
| Code review, PRs, tuning logic | Live Blofin API (`.env`) |
| Read committed docs / scripts | `python scripts/hourly_maintain.py` |
| Memories / checklist | Closing real positions |

**Live trading hourly:** run on your machine:

```powershell
.\run_hourly.ps1
```

Or open this folder in Cursor — the **session hook** runs the `blofin-hourly` skill when due.

Optional: Windows Task Scheduler → `run_hourly.ps1` every hour (local API), while Cursor Automation handles code on GitHub.
