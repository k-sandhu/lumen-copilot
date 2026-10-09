# DOCX candidate extraction v1

Tracking: #673. Depends on ADR-0026 and canonical schema v1. This pure candidate
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
renderer/schema versions and explicit outcome/diagnostics. Downstream chunker,
embedding and persistence identities remain Python's responsibility. Local cell
span annotations distinguish source evidence from repeated derived labels.
Full baseline evaluation, resource measurements and production cutover remain
human merge gates (#670/#687); generated fixtures alone do not authorize them.
