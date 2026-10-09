# CSV/TSV candidate (#675)

The pure parser streams records from decoded bytes. Comma, semicolon, tab and
pipe dialects are scored outside quotes. Unique nonnumeric first-row labels are
heuristic headers; otherwise labels are derived `C1`… . Original fields remain
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
