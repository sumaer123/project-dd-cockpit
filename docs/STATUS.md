# Project DD Kit — Status


<!-- de:current begin — rendered by .docs-toolkit/de_render.py; do not edit inside the markers -->
**Current (auto, 2026-09-28 09:23 IST)**
- **Last deploy:** 2026-09-27 00:00 `7e6691e` → prod — Removed AE first name from a code comment in app.js and gitignored the local .deploy/ ledger dir so it can never land in the public repo
- **Last push:** 2026-09-28 09:23 `91c197cc9bb5` (main, 1 commits) — main 4eed2a0→91c197c (1 commits): Kit 1.1.0: a colleague can finish setup on a fresh Mac
- **Last feature:** none recorded
- **Last session:** none recorded
- **Incomplete sessions (14d):** none
- Query: `python3 .docs-toolkit/de.py status project-dd-kit` · `de.py ask "…" --project project-dd-kit`
<!-- de:current end -->

**Current status (2026-09-28):** Kit 1.1.0 live at commit 91c197c. Public repo: github.com/sumaer123/project-dd-cockpit (code only, all config local in config/territory.json)

## Recent activity

- **2026-09-27 — Protocol F finalize:** Onboarded project-dd-kit docs (DOCS_INDEX.md, REBUILD_SPEC.md, STATUS.md). Public GitHub repo published with scrubbed, config-driven Project DD code. No runtime deploy; all configuration and secrets remain local and git-ignored. Account-nav bug from original Project DD inherited as-is; logged as open item in Project DD docs.

## Deployment

- **Code repo:** [github.com/sumaer123/project-dd-cockpit](https://github.com/sumaer123/project-dd-cockpit) (public, code only)
- **Latest commit:** 91c197c (Kit 1.1.0: non-interactive configure, live access probes, consumption-visibility guard, timezone-correct refresh stamps)
- **Configuration:** Local (config/territory.json, git-ignored; every prompt has a CLI flag via `setup/configure.py --help`)
- **Runtime:** Daily SFDC + Databricks refresh (default 09:00, plus at login) via launchd; optional cloud mirror to Databricks App
