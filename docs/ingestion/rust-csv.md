# CSV/TSV candidate (#675)

The pure parser streams records from decoded bytes. Comma, semicolon, tab and
pipe dialects are scored outside quotes. Unique nonnumeric first-row labels are
heuristic headers; otherwise labels are derived `C1`â€¦ . Original fields remain
in canonical cells; formula-like values are inert strings, never formulas.
Ragged rows remain visible with a diagnostic and a partial outcome. Column type
inference is heuristic metadata, not a value conversion. Record numbers and
physical starting lines are distinct. Unicode renderer spans must round-trip.

`NATIVE_CSV_ENABLED` gates upload admission and cutover. `NATIVE_CSV_SHADOW`
permits offline comparison only; no Python CSV parser exists. Both default false.
An unavailable extension cannot admit this format. Python parsers remain live.

Acceptance: four delimiters and quoted newlines; labelled rows and row/line
locations; inert formula payloads; ragged/decoding diagnostics; input, work,
memory, output and time limits; opt-in admission and typed failures.

Merge gate: hold until measured against the baseline evaluation; a human merges.

## Verification (2026-10-08)

- Rust workspace tests (19), CSV properties, clippy, formatting and all four
  cargo-deny gates passed. Python parser/storage/upload/task checks passed (173;
  5 optional/live skips), then installed CSV/detection checks passed (6).
  Ruff and strict mypy covered the changed boundary.
- Generated CSV and TSV: 100% facts, row associations, order and exact offsets.
  Python arms are unsupported. See [measured rows](reports/csv.json).
  Cold import timings on tiny fixtures establish no throughput target.
- [~] 2026-10-08: full backend suite deferred at 3239 MiB available (<4000);
  residual risk: unrelated regressions await CI.
- [~] 2026-10-08: held-out evaluation, scaling and hard RSS enforcement unmeasured;
  residual risk: cooperative budgets do not bound process RSS.
- [s] 2026-10-08: live verification excluded by task instructions; residual risk:
  no datastore or deployed upload round-trip was exercised.

New crates: csv 1.4.0 (MIT OR Unlicense), sha2 0.11.0 (MIT OR Apache-2.0).
The bridge also uses existing serde_json 1.0.151 (MIT OR Apache-2.0).
