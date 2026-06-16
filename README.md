# Lumen Copilot

Agent-driven repository. **If you are an agent, read [AGENTS.md](AGENTS.md) first** — it is the single source of truth for how work happens here.

The product scope and stack are being defined from user stories; what's intentionally undecided is parked in [docs/specs/0001-open-decisions.md](docs/specs/0001-open-decisions.md). Work is tracked on a GitHub Projects board — see [docs/WORK_TRACKING.md](docs/WORK_TRACKING.md).

## Quick links
- **Contract:** [AGENTS.md](AGENTS.md)
- **How work is tracked:** [docs/WORK_TRACKING.md](docs/WORK_TRACKING.md)
- **Open decisions:** [docs/specs/0001-open-decisions.md](docs/specs/0001-open-decisions.md)
- **Architecture decisions:** [docs/architecture/](docs/architecture/)

## Issue pipeline (TL;DR)
```powershell
powershell -File .\scripts\setup-board-and-labels.ps1      # base labels + board (idempotent)
powershell -File .\scripts\stories-to-issues.ps1           # preview (default)
powershell -File .\scripts\stories-to-issues.ps1 -Execute  # create issues (after stories settle)
```
