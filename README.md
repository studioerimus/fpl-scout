# FPL Scout — cloud daily refresh (GitHub Actions)

This runs your dashboard rebuild every morning on GitHub's servers, so it works with your laptop closed. It fetches the full FPL API, runs the real Python pipeline, commits the fresh dashboard, and (optionally) emails it to you.

## One-time setup (~10 minutes, no coding)

1. **Make a repo.** Go to github.com → New repository → name it `fpl-scout` → **Private** → Create.

2. **Add the files.** On the repo page, *Add file → Upload files*, and drag in all of these (all in the repo root):
   - `cloud_refresh.py`
   - `build_dashboard.py`
   - `scout_template.html`
   - `myteam.json`
   - `odds.json`
   - `lineups.json`
   Commit.

3. **Add the workflow.** *Add file → Create new file*, name it exactly:
   `.github/workflows/daily.yml`
   (typing the `.github/workflows/` part creates the folders). Paste the contents of `daily.yml`, commit.

4. **Turn it on.** Open the **Actions** tab → if prompted, enable workflows → click *FPL Scout daily refresh* → **Run workflow** to test it now. In ~1 minute it fetches FPL, rebuilds, and commits `fpl_dashboard.html` back to the repo. Open that file → *Download raw* to view, or enable Pages (step 6) for a link.

5. **Check your team id.** Open `cloud_refresh.py`, line with `ENTRY = 184589`. That's your team — change it only if it's wrong.

6. **(Optional) A permanent link.** Repo *Settings → Pages → Source: Deploy from a branch → main → /(root) → Save*. After the next run your dashboard is live at `https://<your-username>.github.io/fpl-scout/` and refreshes daily.

7. **(Optional) Email it each morning.** *Settings → Secrets and variables → Actions → New repository secret*:
   - `MAIL_USERNAME` = your Gmail address
   - `MAIL_PASSWORD` = a Google **app password** (myaccount.google.com → Security → App passwords). Not your normal password.
   The workflow then emails you the dashboard + the "what changed" note daily. Skip this and it just commits/serves the file.

## What it does each run
- Fetches the complete FPL bootstrap + fixtures (full data, no truncation).
- Syncs your team name; after GW1 starts it also pulls your live picks (public, no login). Pre-season it keeps the saved `myteam.json`.
- Rebuilds `fpl_dashboard.html` and `index.html`.
- Writes `what_changed.md` by diffing against yesterday.

## Known limits (by design)
- **Feeds are seeded, not live here:** `odds.json` and `lineups.json` ship as the last good snapshot. The core (prices, form, minutes, ratings, optimiser) refreshes fully; the odds/lineups layer stays fixed until refreshed from your browser or a later upgrade. Flag me to wire live odds/lineups into the runner if you want them fresh daily.
- **Desktop artifact:** this path delivers by commit/Pages/email; the Cowork desktop artifact updates when you're next on your Mac.
- **Schedule:** GitHub cron can drift a few minutes and is skipped only if the repo is inactive for 60+ days (a manual run resets that).
