# Cursor Automation: Blofin hourly check (create in UI)

**This file is the prompt template.** Cursor Automations are configured at [cursor.com/automations](https://cursor.com/automations) (or Agents → Automations). They run a **cloud agent** on a schedule — not Windows Task Scheduler.

## Setup (one time)

1. Open [cursor.com/automations](https://cursor.com/automations) → **New automation**
2. **Trigger:** Schedule → **Every hour** (or cron `0 * * * *`)
3. **Repository:** `blofin-auto-trader` (must be on GitHub/GitLab and connected to Cursor Cloud)
4. **Branch:** your trading branch (e.g. `main`)
5. **Tools:** enable shell/terminal if available; **Memories** optional
6. **Permissions:** Private or Team — uses cloud agent billing
7. **Prompt:** paste everything below the `---` line
8. Save and enable

> If the repo is local-only, push it to GitHub first, or use the **local** path: project hook + open Cursor in this folder (see `.cursor/hooks.json`).

---

You are the hourly operator for the Blofin 3R scalper at `blofin-auto-trader`.

Read and follow the project skill: `.cursor/skills/blofin-hourly/SKILL.md` (use the Read tool on that path in the repo).

**Goal:** Check health, close non-50x positions (respect per-symbol exchange caps), run the 15m optimizer pass, append `state/hourly_agent_log.jsonl`, and summarize findings.

**Constraints:**
- Do not change steward harvest logic or commit unless a clear bug fix is required; then open a small PR or report only.
- Do not print or commit API keys.
- If `DRY_RUN=true`, report what you would close but do not close live.
- If the automation cannot run shell against the user's Windows machine, output a clear checklist for the user and set `HOURLY_MAINTENANCE_DUE` in memories for the next IDE session.

**First action:** run `python scripts/hourly_health_report.py` if the environment has Python and network to Blofin; otherwise read `state/hourly_report.json` from the last local run.

**Output:** Short summary (equity, opens, leverage issues, optimizer action). If code changes are needed, open a PR with a minimal diff.
