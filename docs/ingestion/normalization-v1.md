# Normalization and diagnostics v1

Tracking: #668. Evidence/derived-content boundary: ADR-0025/0026.

This is explicit native candidate computation; live Python parsing is unchanged.
Canonical evidence and its spans stay byte-for-byte/code-point-for-code-point
intact. Normalized block text is a separate derived representation with block
lineage, never assigned original citation offsets. The generation records the
normalizer policy/build, language-detector identity and diagnostics. Operational
stage persistence belongs to #669; persisting these native candidate outputs
requires the approved format integration in #687. The administrator surface owned
by #628 can project retained diagnostics additively.

Policy: NFC rather than NFKC; preserve numbers, identifiers, unit symbols,
table grids/cells and code. Normalize CRLF to LF in prose; collapse horizontal
ASCII spaces only between prose words. Expand typographic alphabetic ligatures
only in lowercase alphabetic prose tokens. Remove a soft discretionary hyphen
before a line break only between lowercase alphabetic prose fragments. Preserve
hard hyphens (including identifiers and compound words), warning on ambiguous
line-break hyphenation. This deliberately does not guess whether a hard hyphen
belongs to an identifier or word. Normalization never calculates numeric values,
formulas, units, OCR corrections or missing text.

Exclude furniture from retrieval only when a supplied Furniture block has the
same exact text on at least two distinct supplied page regions. A repeated body
paragraph, one-page furniture or unknown page number is never excluded. Keep
excluded source blocks/spans for inspection and citations. The chunker consults
the exclusion ids but quotes the original source for everything it emits.

Per-page inspection uses supplied source_parts, including empty pages. Report
characters, text presence, replacement/control counts, mojibake indicators,
garbled ratio, language (unknown unless reliable), warnings and cell-text
presence counts. Table presence is not association accuracy. Empty text layers
warn `no_native_text_layer`; scans versus intentional blanks stay unknown.
Suspect/empty output is a partial/empty inspection outcome, never a fidelity or
OCR-success claim. Language identification is heuristic, sampled and bounded.

Acceptance: identifier/number/unit and compound-word negatives, ligatures/soft
hyphens, repeated-furniture/body distinctions, unknown pages, empty/garbled page
warnings, language/coverage, original Unicode span properties and cancellation.
