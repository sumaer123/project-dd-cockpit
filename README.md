# Project DD — Territory Cockpit for Sales Managers

A territory cockpit for a Databricks sales manager and their AEs. It pulls live
Salesforce data (accounts, use cases, opportunities, ConsumptionPlan forecasts
and quotas) and Databricks consumption data, builds one static web app, and
optionally mirrors it as a read-only **Databricks App**.

**Version:** 1.1.0 (see `VERSION`).

- **No LLM and no tokens.** The whole pipeline is deterministic Python (stdlib only) plus the `sf` and `databricks` CLIs.
- **No secrets in the repo.** Logins stay inside the `sf` and `databricks` CLIs (browser SSO / OAuth).
- **No hard-coded people.** `setup/configure.py` reads *your* role and *your* team from Salesforce and writes config/territory.json, which is git-ignored. Every other file reads that config. Every question has a flag; run `setup/configure.py --help` to see them.
- **No data in the repo.** Everything generated (raw pulls, app/data.js) is git-ignored.

## Quick start (macOS)

```bash
git clone https://github.com/sumaer123/project-dd-cockpit.git ~/project-dd
cd ~/project-dd
./setup/bootstrap.sh
```

`bootstrap.sh` installs any missing tools, finds your role and team in Salesforce,
runs the first data pull, installs the daily refresh, and (optionally) creates
the cloud app. **The full step-by-step guide, including prerequisites, access
and troubleshooting, is the setup guide you were sent with this link.**

## What's in the app

| Module | What it shows |
|---|---|
| Consumption / Territory | Run-rates (T7D/T28D), QoQ, central forecast table (QTD · GTTB · AE forecast · target · gap), product mix, per-AE and per-account drill-downs |
| Quarter Scorecard | Per-AE QoQ growth band (current-quarter forecast vs prior-quarter GTTB) and the go-live book |
| Opportunities | Open pipeline by close quarter and forecast category, plus closed-won with IARR and tenure |
| New Pipeline | New opps, new use cases and stage progressions, week by week |
| Pipeline Coverage | Per-AE pipe vs pipe needed for this quarter and next (in-quarter-revenue weighted) |
| RoB — weekly review | Friday review for your manager: top go-lives, commit opps, week-over-week diff, your narrative, and an export |
| Partner Exposure | Partners and SIs named across the open use-case book, each with its source line |

## Layout

```
setup/      configure.py (role + team discovery) · bootstrap.sh · install_schedule.py · create_cloud_app.py
pipeline/   refresh.py (the pull) · ddcore.py (auth, errors, locking) · ddconfig.py (config loader)
            ddmodules.py (post-build checks) · refresh_server.py (localhost helper for "Refresh now")
data/       build_data.py (turns raw pulls into app/data.js)
app/        index.html · app.js · styles.css (the cockpit, opened from file://)
cloud/      server.py + app.yaml (Databricks App) · build_cloud.py · deploy_cloud.py
config/     territory.example.json (schema). Your territory.json is written here and git-ignored.
```

## Day to day

- **Open:** double-click `Open Project DD.command`.
- **Fresh data now:** double-click `Update Project DD.command`, or click **Refresh now** in the app.
- **Daily:** the launchd job refreshes at the configured hour (default 09:00) and at login.
- **Team changed?** Re-run `setup/configure.py`.
