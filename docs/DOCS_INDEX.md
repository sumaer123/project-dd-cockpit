# Project DD Kit — Documentation Index

**Project:** Project DD Kit — Config-driven self-installable territory cockpit for sales managers
**Root:** `/Users/sumaer.bahl/Downloads/Sumaer's Claude Data/project-dd-kit/`
**Indexed:** 2026-09-27

---

## Part A — The Map

### Entry Points

| Doc (path) | Purpose | Status | Last updated |
|---|---|---|---|
| `README.md` | Project overview, quick start, key features (local app + cloud Databricks App mirror), what's different from Project DD | CURRENT | 2026-09-27 (onboarded) |
| `docs/DOCS_INDEX.md` | This index — project documentation map with entry points, design docs, and revision history | CURRENT | 2026-09-27 (created) |

### Design / Specification Docs

| Doc (path) | Purpose | Status | Last updated |
|---|---|---|---|
| `docs/REBUILD_SPEC.md` | Clean-machine rebuild spec: configuration (territory.json), environment variables, build & refresh procedures, launchd schedule, Databricks App setup | CURRENT | 2026-09-27 (created) |
| `docs/STATUS.md` | Project status and deployment info: public GitHub repo link, recent activity log | CURRENT | 2026-09-27 (created) |

### Related Documentation

| Doc (path) | Purpose | Status | Last updated |
|---|---|---|---|
| `Project DD Handover/Project_DD_Setup_Guide.md` (external) | Step-by-step onboarding for a new territory manager (60–90 min, includes SFDC/Databricks logins). Internal only — contains workspace URLs and IDs. | REFERENCE | 2026-09-27 (linked) |

---

## Setup & Installation

**Primary guide:** [`Project_DD_Setup_Guide.md`](../../Project%20DD%20Handover/Project_DD_Setup_Guide.md) — step-by-step onboarding for a new territory manager (60–90 min, includes all logins and configuration). Kept **internal only** — contains workspace URLs and IDs.

**Public repo:** [github.com/sumaer123/project-dd-cockpit](https://github.com/sumaer123/project-dd-cockpit) (code only, no data/names/IDs; all config lives in git-ignored `config/territory.json`).

---

## What's Different from Project DD

- **Configuration-driven:** Territory specifics (people, AE roster, account map, manager info) live in `config/territory.json` (git-ignored), written by `setup/configure.py` from the installing manager's SFDC User record and role.
- **Self-contained launchd:** `setup/install_schedule.py` generates the daily refresh schedule; fiscal year is computed from the date.
- **Optional modules:**
  - BABA deep-dive (now optional via `DD_DEEPDIVE_URL` env var; omit if not needed).
  - DobbyNXT hand-off (removed; integrate separately if desired).
  - Hygiene module (hidden when `hygiene_data.js` is absent).
  - RoB narrative (optional via `app/rob_content.js`; can be provided externally).
- **Cloud app creation:** `setup/create_cloud_app.py` automates first Databricks App create + deploy (run once).
- **Verification:** Every build runs `ddmodules.py` (11 checks, all or nothing) to confirm each module has data.

---

## Repository Structure

| Path | Purpose |
|---|---|
| `setup/bootstrap.sh` | One-command orchestrator (safe to re-run). |
| `setup/configure.py` | Interactive config builder — logs into SFDC, finds your reports, confirms AEs, checks Databricks. Outputs `config/territory.json`. |
| `setup/install_schedule.py` | Installs launchd for daily refresh. |
| `setup/create_cloud_app.py` | Creates Databricks App, runs first deploy (one time only). |
| `pipeline/refresh.py` | Nightly data pull: SFDC + Databricks in parallel, build, verify, publish. |
| `pipeline/ddconfig.py` | Loads `config/territory.json`; no other file hard-codes names/IDs/workspace. |
| `pipeline/ddcore.py` | Login checks, typed errors, retries, locking, progress. |
| `pipeline/ddmodules.py` | Post-build verification (11 checks per module). |
| `app/` | Frontend (same as Project DD, reused). |
| `cloud/` | Databricks App code (mirrors local app). |

---

## Open Items

- **Original Project DD bug:** Account-nav crash on clicking a child SFDC account ID in RoB (renderTopbar: acctMap undefined in `app/app.js`). This kit inherits the bug as-is. Log in Project DD docs if still unresolved.

---

## Part B — The Log

| Date | Event | Docs touched | By |
|---|---|---|---|
| 2026-09-27 (deploy finalize) | **Onboarding finalize: Project DD Kit published.** New public GitHub repo [sumaer123/project-dd-cockpit](https://github.com/sumaer123/project-dd-cockpit) (code only, no data) — config-driven, scrubbed copy of Project DD for sales managers to self-install. Territory cockpit with SFDC + Databricks nightly refresh; local web app + Databricks App cloud mirror. All configuration & secrets stay local in git-ignored `config/territory.json`. Setup guide (internal, contains workspace URLs/IDs) at `/Users/sumaer.bahl/Downloads/Sumaer's Claude Data/Project DD Handover/Project_DD_Setup_Guide.md`. Onboarding docs created: Part A indexing, REBUILD_SPEC (configuration + build procedures), STATUS (deployment info). Registry entry added. Project DD docs updated with cross-reference noting the public kit and open account-nav bug. User-facing: NO (code repo only). | DOCS_INDEX.md (Part A indexing + Part B created), REBUILD_SPEC.md (created), STATUS.md (created) | documentation-engineer (deploy finalize) |
