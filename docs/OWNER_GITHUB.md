# Owner — publish God Bot on GitHub for friends

## One-time publish (this machine)

Prerequisites: [GitHub CLI](https://cli.github.com/) — `gh auth login`

```powershell
cd C:\Users\mknig\blofin-auto-trader
git add -A
git status   # confirm NO .env, state/, logs/, *.gguf
git commit -m "God Bot: holistic repo docs, bootstrap, friend setup"
powershell -ExecutionPolicy Bypass -File .\scripts\publish_github.ps1
```

Default remote repo name: **`blofin-auto-trader`** under your GitHub user.

Repo URL (current): `https://github.com/1bananaonthewall-ux/blofin-auto-trader`

## Give a friend access

**Private repo (recommended):**

```powershell
gh repo collaborator add GITHUB_USERNAME --repo 1bananaonthewall-ux/blofin-auto-trader
```

Or: GitHub web → repo **Settings → Collaborators → Add people**.

**Public repo:** anyone can clone; keys still stay in each user’s `.env` only.

## What friends clone

They get:

- Full God Bot code + `God Bot.ps1` + dashboard + `.cursor` agent assets
- `.env.example` (identical profile, empty keys)
- `docs/GETTING_STARTED.md`, `AGENT_READ_ME_FIRST.md`
- `scripts/bootstrap_god_bot.ps1`

They do **not** get your `.env`, `state/`, `logs/`, or GGUF weights (all gitignored).

## Tell your friend

Send them:

1. Repo URL: `https://github.com/1bananaonthewall-ux/blofin-auto-trader`
2. Link to **docs/GETTING_STARTED.md**
3. “Install Cursor, clone repo, run `scripts\bootstrap_god_bot.ps1`, put **your** Blofin API in `.env`, then `God Bot.ps1 -Action ensure`.”

## Keeping repos in sync

After you change code:

```powershell
git add -A
git commit -m "describe change"
git push origin main
```

Friend updates:

```powershell
git pull
powershell -ExecutionPolicy Bypass -File .\scripts\stack_control.ps1 -Action restart-fresh
```

## Security checklist before push

- [ ] `.env` is **not** staged (`git check-ignore -v .env`)
- [ ] No API keys in committed files (`scripts/godbot_audit.py` optional)
- [ ] `state/` and `logs/` not tracked
- [ ] `models/*.gguf` not tracked
