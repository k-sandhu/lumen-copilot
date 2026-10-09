# DOCX candidate extraction v1

Tracking: #673. Depends on ADR-0027 and canonical schema v1. This pure candidate
does not change Python parsing, upload admission, persistence or cutover flags.

Acceptance follows issue #673 and spec 0010: body order, sparse and merged grid
positions, scalar-only nested aliases, source header labels and bounded work.
Canonical block separators are two newlines, rather than Python's one newline;
compare block content and table rendering separately from that versioned change.
DOCX has no invented page numbers. Native regions name the package part and
paragraph/table/cell path. Structured cells retain only merge origins and spans.

Policy: accepted text includes insertions and move destinations, excludes
deletions and move sources. Comments are annotations, never body evidence.
Referenced footnotes/endnotes attach to their reference block; referenced
headers/footers appear once as furniture. Text boxes are retained, and images
have source alt-text placeholders without OCR claims. Supplied styles/outline
levels and numbering definitions determine headings/list nesting; unknown stays
unknown. Hyperlinks retain display text and inert target annotations; nothing
is fetched. No source content or target is logged in errors.

DOCM and packages declaring VBA fail with typed unsupported, including packages
disguised as DOCX. DOTX uses the same text path. ZIP traversal, duplicate parts,
encryption, excessive entries/expansion/ratio, DTD/entities, malformed XML and
corrupt packages fail closed. Limits are engineering profiles, not production
capacities. Entry points also accept the runtime Context for cancellation.

Generation metadata retains source SHA-256, parser/build/dependency identities,
renderer/schema versions and explicit outcome/diagnostics. Build identity hashes
format code, shared package/canonical/runtime code and the pinned dependency graph.
Downstream chunker,
embedding and persistence identities remain Python's responsibility. Local cell
span annotations distinguish source evidence from repeated derived labels.
Full baseline evaluation, resource measurements and production cutover remain
human merge gates (#670/#687); generated fixtures alone do not authorize them.

Verification on 2026-10-08: focused Rust suite passed (6 tests, including 256
Unicode/table property cases); clippy all targets passed; cargo-deny 0.20.2
licenses/advisories passed offline. Existing Python fixtures are replayed through
`backend/tests/test_rust_docx_parity.py` with the `docx_extract` example driver.
All 14 comparisons pass exactly after projecting canonical block separators
back to the baseline, including the 1,000-row vertical merge. The combined Python
regression/parity run has 49 passing nodes. Separate dependency #708 / PR #712
replaces quadratic canonical overlap validation with a bounded rectangle sweep;
its four regression/property tests pass.
Joint Office checkout: all 32 core Rust tests and Clippy all targets pass offline.
Dependency #713 / PR #714 pins the existing local bridge dependency; complete
cargo-deny checks (advisories, bans, licenses, sources) pass without policy changes.

[~] 2026-10-08: full offline backend suite deferred because available RAM was
below 4000 MiB. Residual risk: unrelated regressions are not exhaustively checked.
[~] 2026-10-08: live gates, held-out fidelity, RSS/throughput and cross-platform
wheels unverified under this task's stack restrictions. Residual risk: generated
fixture results do not establish production fidelity or capacity. UTF-16 XML,
ZIP64 and unsupported embedded content need further fixtures before promotion.
The bounded XML event reader currently retains a per-part tree; memory accounting
is conservative and is not a process RSS or hard native interruption guarantee.
