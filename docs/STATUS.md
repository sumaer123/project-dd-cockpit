# Project DD Kit — Status

**Current status (2026-09-27):** Public repo live: github.com/sumaer123/project-dd-cockpit (commit 4d0bb17)

## Recent activity

- **2026-09-27 — Protocol F finalize:** Onboarded project-dd-kit docs (DOCS_INDEX.md, REBUILD_SPEC.md, STATUS.md). Public GitHub repo published with scrubbed, config-driven Project DD code. No runtime deploy; all configuration and secrets remain local and git-ignored. Account-nav bug from original Project DD inherited as-is; logged as open item in Project DD docs.

## Deployment

- **Code repo:** [github.com/sumaer123/project-dd-cockpit](https://github.com/sumaer123/project-dd-cockpit) (public, code only)
- **Latest commit:** 4d0bb17
- **Configuration:** Local (`config/territory.json`, git-ignored)
- **Runtime:** Nightly SFDC + Databricks refresh via launchd + Databricks App cloud mirror
