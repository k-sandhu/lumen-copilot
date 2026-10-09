# Text/Markdown/code candidate (#680)

The candidate decodes bounded bytes with the foundation's encoding detector,
counts decoding errors, normalizes CRLF/CR to LF, and keeps decoded source-line
locations separate from Unicode renderer offsets. Text paragraphs split on blank
lines. Markdown uses supplied heading/list/code roles; fenced and indented code
remain whole canonical blocks. Source MIME types supply language labels and
render the entire file as one code block. No code executes.

Both NATIVE_TEXT_ENABLED and NATIVE_TEXT_SHADOW default false. Shadow failures
retain the live Python result and emit only safe counts/error categories. Common
code MIME types are admitted only with enabled cutover and an installed wheel.
Binary signatures/control-rich content are rejected even with text MIME types.
Chunking remains the separately tracked #667; this parser preserves code units.

Acceptance: heading hierarchy, lists, intact code, source language, legacy/BOM
decoding diagnostics, line normalization and exact offsets, binary and budget
negatives, independent admission and shadow rollback.

Merge gate: hold until measured against the baseline evaluation; a human merges.

## Verification (2026-10-08)

Three Rust tests (including generated Unicode/line properties), clippy, fmt and
all four cargo-deny checks pass. Installed wheel: 19 targeted Python tests pass.
Ruff passes. Six baseline fixtures yield 100% candidate facts/order/offsets:
UTF-8/Markdown parity, UTF-16 (Python 0% facts) and Windows-1252 (Python 83.3%
facts), multilingual and empty outcomes. [Measured rows](reports/text.json).

New crates: pulldown-cmark 0.13.4 (MIT), sha2 0.11.0 (MIT OR Apache-2.0);
bridge reuses serde_json 1.0.151 (MIT OR Apache-2.0).

- [~] 2026-10-08: full backend suite deferred at 1679 MiB available (<4000);
  residual risk: unrelated regressions await CI.
- [~] 2026-10-08: held-out quality/performance/RSS and structure-aware pipeline
  integration (#667–#669) remain gated; residual risk: the existing flattened
  ingestion pipeline cannot persist all canonical structure or guarantee atomic
  code chunks until those dependent stages are integrated.
- [s] 2026-10-08: live gates excluded by task; residual risk: no deployed round-trip.

Strict mypy using backend/pyproject.toml: 9 changed source files passed.
