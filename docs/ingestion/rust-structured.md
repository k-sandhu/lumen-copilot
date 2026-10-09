# Structured candidates (#683)

Acceptance: JSON keys retain source order and JSON Pointer paths escape ~ and /.
JSON Lines is processed one physical line/record at a time; blank lines retain
physical line numbering. Readable records include key paths and values; metadata
exposes typed values and exact block-local Unicode value spans. Aggregated output
is bounded; records larger than max_record_bytes fail without silent truncation.

XML accepts UTF-8 (or ASCII) declarations and uses streaming namespace-aware events and Clark-name sibling paths, UTF-8
source line/byte locations and supplied attributes. DTDs are unsupported; custom
entities fail. No entity expansion, resource fetch or schema-specific calculation.
Depth, input, retained memory, output, work and deadline limits produce typed errors.

XBRL and inline XBRL retain supplied concept, lexical value, context/unit references,
resolved periods/units where present, format/scale/sign attributes and fact paths.
Late contexts/units are resolved after streaming; missing values remain unknown.
Inline content is well-formed XML/XHTML; scripts/styles are omitted. Numeric
transformation and continuation composition are outside this conservative profile
and produce an incomplete result when present. Tagged facts have exact value spans
and block provenance alongside readable source text. Nothing executes.

NATIVE_JSON/JSONL/XML/XBRL/IXBRL_ENABLED and matching SHADOW flags default false.
New types are admitted only for enabled cutover with a capable installed wheel.
Partial results cannot enter the live path; Python remains the default.

Merge gate: hold until measured against the baseline evaluation; a human merges.

Production routing also awaits namespace-aware recognition #733: pure parsers
resolve arbitrary prefixes, while the foundation detector uses literal markers.
