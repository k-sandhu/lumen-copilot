# Canonical computation contract v1 (#664)

ADR-0025 and ADR-0027 define the semantics. This internal, offline contract adds
no API, storage migration, retained-generation policy or format parser.

A Document has `schema_version=1`, `renderer_version=1`, ordered `blocks`,
`source_parts` and `generation`. A Block has a stable unique `id`, a `kind`,
`text`, nullable `parent_id`, heading level (headings only), heading path,
regions, optional table and origin (`source`, `heuristic`, `derived`). The flat
ordered block list determines reading order; parent links determine hierarchy.
Children do not duplicate their parent's text. Unknown roles/locations stay null.
Kinds: heading, paragraph, list, table, figure, caption, footnote, code, furniture.

Table cells carry 1-based row/column, positive row/column spans, text, supplied
header role or unknown, formula/cache freshness/format/unit metadata. Sparse
blank positions are retained by table dimensions; overlapping origin cells are
invalid. Rendered block text is supplied by the format adapter; rendering here
joins block text with two newlines without guessing table labels or adding
retrieval context. Derived wrappers never become evidence offsets.

Source regions carry optional page/slide/sheet identity, cell range and bounding
box with required coordinate origin/unit. Ordered source parts carry the Python
provenance shape `kind`, `name`, `number`, `char_start`, `char_end`; empty parts may
be zero-width. Regions and source parts are separate from block spans.
Generation carries optional original SHA-256/extraction ID, parser/build/renderer
identities, dependencies, and fingerprint/diagnostics/outcome metadata. Legacy
unknown values stay null; metadata describes supplied provenance, not readiness.
Python alone owns outcome publication, permissions, persistence and activation.

Document-level container children are not represented by this v1 contract.
The [container child-document proposal](container-children-proposal.md) (#682)
defines a separate proposed bundle for owner review; it is not an implemented
schema extension. `Block.parent_id` continues to refer only to another block.

Render returns the document, exact `rendered_text` and one span per block. Offset
units are Unicode scalar values (Python str code points for valid Unicode);
unpaired surrogates are rejected by JSON/native input validation. Empty block
spans are allowed. Duplicate IDs, orphan/self/cyclic parents, inconsistent
heading/table fields, overlapping cells, invalid coordinates or invalid source
part ranges fail with typed errors. Serialization round trips preserve all fields.

Safety caps for standalone rendering: 32 MiB JSON input, 100000 blocks, 2 million
rendered code points and 100000 table-intersection comparisons. Exceeding a cap
is a typed budget failure, never truncated success. Runtime budgets (#666) add
cooperative cancellation/deadline checks around format work.

Parity tests pass each current Python format's exact extracted text into a
single Rust paragraph and compare the retained rendering side by side. This
checks the model/offset boundary, not native format extraction fidelity. No Rust
format parser is claimed; #670 reports unavailable candidate arms explicitly.

The JSON boundary preflights collections without constructing the model. Its
initial profile caps 100,000 JSON values and conservatively reserves 32 MiB of
headroom plus twice input bytes plus 512 bytes per JSON value against 128 MiB.
This includes an allowance for validation/render/serialization structures; it
is an estimate, not a process RSS cap. The node cap tightens as input grows.
Oversized compact metadata/cell arrays fail typed Budget before their model
collections allocate. This can reject a shape below the separate block-count
ceiling; changing the profile requires measured evaluation. Tests never print
whole document JSON on budget failures.
