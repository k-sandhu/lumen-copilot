# Backend test runtime (#655)

The offline suite must collect and retain every existing test and its outcome.
Optimization changes scaffolding, never assertions, skip gates, or production
security parameters. The issue's acceptance criteria authorize these changes.

- Default to four xdist workers, overridable with `PYTEST_ADDOPTS="-n 2"` or an
  explicit `-n` argument. Run only one pytest invocation at a time, without other
  heavy commands. Avoid `-n auto` on shared hosts. Use `loadfile`; keep each
  module's fixture lifecycle and tests on one worker. Each invocation has a unique base temporary directory,
  with xdist's worker subdirectories beneath it.
- Build the complete SQLite schema once per worker session. Restore it into each
  test's existing connection using SQLite backup. Rows, transactions, pool type,
  connection hooks, and engine teardown remain owned by the original fixtures.
  PostgreSQL and deliberate DDL/migration checks still execute real DDL.
- `ENVIRONMENT=test` enables minimal Argon2id cost by default. An explicit
  `TEST_FAST_PASSWORD_HASHING=true` is rejected in every other environment.
  `false` retains production cost even in test. Outside test the existing
  Argon2id parameters remain time=3, memory=65536 KiB, parallelism=4, hash=32
  bytes, salt=16 bytes. The dummy hash uses the same selected hasher.
- Track event-loop construction, holding loops until deterministic teardown.
  Close idle loops without scanning unrelated objects or forcing collection.
  Running loops remain open. No warning suppression replaces #94's protection.
- Opt-in PostgreSQL fixtures use worker-qualified disposable database names.
  Offline execution continues to collect and skip live tests without secrets.

CI runs on pull requests and pushes to `main`. The backend job installs locked
uv development dependencies, then runs Ruff lint, Ruff formatting, strict mypy
on `app`, and the full offline pytest suite with xdist. The frontend job installs
frozen pnpm dependencies, then runs lint, typecheck, Vitest, and build. Both jobs
cache dependency downloads, use official actions pinned to a major version, and
receive no secrets. This implements OD-7's fast-gate parity for #655; the
protected agent contracts' stale CI wording is proposed for owner approval in
the PR rather than edited here.

Verification includes before/after node-id and JUnit outcome comparisons,
single-process and parallel timings on the same shared machine, repeated full
`-W error` runs (including shuffled module order), and temporary regressions of
the hashing gate and loop cleanup that must turn their regression tests red.

For the strict warning runs on the pinned Python 3.12 dependencies, use `-W error`
with these two specific upstream deprecation exceptions:

```text
-W "ignore:open_text is deprecated:DeprecationWarning"
-W "ignore:datetime.datetime.utcnow():DeprecationWarning:botocore.auth"
```

These cover LiteLLM's legacy resource loader and Botocore's signing clock.
`ResourceWarning` and pytest's unraisable-exception warnings remain errors.
