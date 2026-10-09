# Structure chunking v1

Tracking: #667. Boundary and evidence policy: ADR-0025/0026 and spec 0009.

The native candidate is explicit opt-in computation, not a production cutover.
Python still chunks live ingestion until #687 approves each format. Python reads
a local tokenizer JSON; no hub client or test-time download is used. Configure
the matching embedding model identity and SHA-256 of that artifact. The current
backend default is `openai/nvidia/nemotron-3-embed-1b:free`; a bge-m3 evaluation
must explicitly select its model and tokenizer together. Never assume a tokenizer
family from a provider routing name. Fingerprints record model, artifact hash,
tokenizers version and chunker settings; local filesystem paths are excluded.

Each chunk quotes an exact Unicode code-point slice of deterministic canonical
text. Blocks delimit structural units; paragraphs/list/code may split at sentence
or whitespace boundaries. Within a split block, character overlap is measured
from the actual cut, with strictly increasing starts and ends. Block boundaries
reset overlap (explicit structural discontinuity). Blank-only blocks are omitted.
The full context plus evidence is token-counted with special tokens, without
truncation/padding. Oversized atomic units or overlap that prevents progress
return a typed budget error, never silently drop evidence or reduce overlap.

Tables are atomic rows. Their supplied grid must render as tab-separated cells
and newline-separated rows, including blank/merged positions; an unknown mapping
is rejected rather than guessed. A row is never divided. Header cells (including
spanning row labels), supplied caption and units accompany evidence as separate
context, repeated on each applicable row. Heading breadcrumbs likewise remain
context. Context carries cell/block lineage, never canonical citation offsets.
No value is separated from its row label/unit. An oversized row fails explicitly.

Acceptance: generated offline tokenizers; exact non-ASCII slices; full nonblank
coverage; token limits including context/special tokens; early-boundary overlap
regression; progress/offset proptests; table row/header/unit preservation; invalid
settings, mapping, tokenizer and budget/cancellation negatives. Retained generation
identity is additive; existing citation offsets and Python parser behavior stay live.
