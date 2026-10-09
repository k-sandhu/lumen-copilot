# Structured candidates (#683)

Acceptance: JSON keys retain source order and JSON Pointer paths escape ~ and /.
JSON Lines is processed one physical line/record at a time; blank lines retain
physical line numbering. Readable records include key paths and values; metadata
exposes typed values and exact block-local Unicode value spans. Aggregated output
is bounded; records larger than max_record_bytes fail without silent truncation.

XML accepts UTF-8 (or ASCII) declarations and uses streaming namespace-aware
events and Clark-name sibling paths, UTF-8 source line/byte locations and supplied attributes. DTDs are unsupported; custom
entities fail. No entity expansion, resource fetch or schema-specific calculation.
Depth, input, retained memory, output, work and deadline limits produce typed errors.

XBRL and inline XBRL retain supplied concept, lexical value, context/unit references,
resolved periods/units where present, format/scale/sign attributes and fact paths.
Late contexts/units are resolved after streaming; missing values remain unknown.
Inline content is well-formed XML/XHTML; scripts/styles are omitted. Numeric
transformation, continuation and exclusion composition are outside this conservative profile
and produce an incomplete result when present. Tagged facts have exact value spans
and block provenance alongside readable source text. Nothing executes.

NATIVE_JSON/JSONL/XML/XBRL/IXBRL_ENABLED and matching SHADOW flags default false.
New types are admitted only for enabled cutover with a capable installed wheel.
Partial results cannot enter the live path; Python remains the default.

Merge gate: hold until measured against the baseline evaluation; a human merges.

Production routing also awaits namespace-aware recognition #733: pure parsers
resolve arbitrary prefixes, while the foundation detector uses literal markers.

## Dependencies and verification (2026-10-09)

New direct core crates: quick-xml 0.42.0 (MIT), sha2 0.11.0 (MIT OR Apache-2.0).
Bridge reuses serde_json 1.0.151 (MIT OR Apache-2.0); preserve_order uses the
already pinned indexmap 2.14.2 (MIT OR Apache-2.0). All four cargo-deny checks pass.

[x] 2026-10-09: seven Rust tests passed, including JSON/element/fact Unicode
path/span properties, 400-record streaming within 16 MiB accounting, entity/depth/
output/memory negatives and text coalescing regressions. Clippy/all-targets,
fmt and Ruff pass. Native Windows wheel rebuilt/imported.
[x] 2026-10-09: 23 scoped Python tests passed; 7 candidate tests repeated after
XML corrections passed. Candidate runner reports are checked in per family.
Primary JSON/JSONL/XML/XBRL/inline XBRL fixtures score 100% facts, associations,
order and exact offsets. XML entity fixture is rejected. Non-fact generic XML
and HTML/XHTML routing fallback probes are rejected by tagged-fact parsers; their
0% rows remain visible in reports and are not pooled into primary-format scores.
Python does not support these formats. No performance claim from tiny fixtures.

[~] 2026-10-09: full offline backend suite started at 4461 MiB, then stopped
after a foundation tmp_path setup error (WinError 5 on the shared pytest temp
root). The failing test passed with an isolated task temp root. Remaining broad
coverage awaits CI; this PR has made its one full-suite attempt. Residual risk:
broader integration regressions remain unverified. Promotion awaits detection #728/#733, canonical
stages #667–#669 and held-out quality/performance/RSS evaluation. Residual risk:
caller-budget routing, specialized format selection and persisted structure.
[s] 2026-10-09: live service tests excluded by task; deployed behavior unverified.

[x] 2026-10-09: backend-configured strict mypy passed for 13 source files.

[x] 2026-10-09: exclusion regression failed before adding explicit incomplete
diagnostics. Eight Rust and 7 installed structured Python tests now pass, with
clippy/all-targets and Ruff green. Inline composition follow-up: #736. Shared
binary-signature helper is identical to the other format branches.

[x] 2026-10-09: numeric precision regression failed before enabling serde_json
arbitrary_precision. Ten structured, five canonical and four detection Rust
tests now pass; 16 installed structured/canonical Python tests pass. Clippy,
Ruff and all four cargo-deny checks pass; the Windows native wheel was rebuilt.
JSON/JSONL numeric fields retain a precision-preserving `number_text` string
alongside the typed numeric `value`, with exact rendered value spans. Python's
JSON view retains arbitrary integers but converts decimal numeric values to
binary floats; consumers requiring decimal precision must use `number_text`.
No additional crate or licence was introduced by the serde_json feature.
