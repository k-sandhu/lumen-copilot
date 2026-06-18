# Lumen Copilot

Agent-driven repository. **If you are an agent, read [AGENTS.md](AGENTS.md) first** — it is the single source of truth for how work happens here.

The product scope and stack are being defined from user stories; what's intentionally undecided is parked in [docs/specs/0001-open-decisions.md](docs/specs/0001-open-decisions.md). Work is tracked on a GitHub Projects board — see [docs/WORK_TRACKING.md](docs/WORK_TRACKING.md).

## Quick links
- **Contract:** [AGENTS.md](AGENTS.md)
- **How work is tracked:** [docs/WORK_TRACKING.md](docs/WORK_TRACKING.md)
- **Open decisions:** [docs/specs/0001-open-decisions.md](docs/specs/0001-open-decisions.md)
- **Architecture decisions:** [docs/architecture/](docs/architecture/)

## In-app developer pages
The frontend SPA ships two developer-facing pages, reachable from the main app via a floating "🧭 Pages" overlay (hover or focus):
- **`/docs`** — renders every markdown file under `docs/` (plus the AGENTS/README contracts), bundled at build time and shown through the sanitized markdown pipeline; doc-to-doc links navigate in-app.
- **`/features`** — a curated, grounded catalog of what's shipped, each entry linking to its ADR/spec/PR.

Keep `/features` honest: when a feature lands, update `frontend/src/features/feature-catalog/catalog.ts` in the **same** PR (a test fails if any catalog→doc link rots). How docs are bundled into the static SPA: [frontend/src/features/docs/README.md](frontend/src/features/docs/README.md).

## Issue pipeline (TL;DR)
```powershell
powershell -File .\scripts\setup-board-and-labels.ps1      # base labels + board (idempotent)
powershell -File .\scripts\stories-to-issues.ps1           # preview (default)
powershell -File .\scripts\stories-to-issues.ps1 -Execute  # create issues (after stories settle)
```
