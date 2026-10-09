# 26. Continuous integration

- **Status:** Accepted (explicit sponsor instruction in #655)
- **Date:** 2026-10-02
- **Tracking:** [#655](https://github.com/k-sandhu/lumen-copilot/issues/655)
- **Closes:** OD-7

## Decision

GitHub Actions runs two independent jobs on every pull request and on pushes to
`main`. The backend installs locked uv development dependencies, checks Ruff
lint and formatting, runs strict `mypy app`, and executes the full offline pytest
suite with two xdist workers and `loadfile`. The frontend installs frozen pnpm
dependencies, then runs lint, typecheck, Vitest, and build. These are the same
commands developers run locally.

Use only official GitHub actions pinned to major versions. Install pinned uv and
pnpm releases directly and cache their download stores with lockfile-based keys.
Grant only repository read access. Cancel superseded runs for the same ref.
No service containers or secrets are required: existing live-test opt-ins remain
off. Backend warning handling continues to fail socket leaks.

Four workers bound memory on shared developer machines; CI uses two workers on
its smaller runners. `PYTEST_ADDOPTS` or an explicit `-n` overrides the local default.
The test-runtime isolation and security criteria are documented in
[the runtime guide](../guides/backend-test-runtime.md). Dependency-light smoke
scripts and the remaining harness work still belong to OD-6.

The agent contracts are protected by the session's explicit instruction. Their
stale CI/test-command wording is proposed in the PR for owner approval, rather
than edited as part of this change.
