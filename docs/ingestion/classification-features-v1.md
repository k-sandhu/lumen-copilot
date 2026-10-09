# Classification evidence — #691

ADR-0028 and ADR-0027 govern this candidate computation. Python supplies one
authorized canonical generation, an approved local tokenizer and immutable
taxonomy-versioned literal rules. No model, storage or network call occurs in Rust.

Features include format, page/slide/sheet counts, table density, heading levels,
form-layout indicators, supplied language and supplied OCR scanned ratio.
Blank pages do not prove scanning; absent metadata remains unknown. Fixed literal
signatures cover explicit form/report identifiers or citations supplied as data.
The initial conservative safety-sheet rule requires four independent headings.
No new dependencies are introduced.

Excerpt candidates prioritize title/first block, headings, table-header blocks,
signature blocks and first/middle/last representative blocks. Each candidate is
at most 512 Unicode code points, with exact original block/region/span provenance.
The final joined evidence is counted by the supplied tokenizer, with truncation
and padding disabled. Caller instruction/options are not included in this evidence
budget: #692 must separately count the complete request before model dispatch.
Excerpts never serialize complete rendered source. Existing source text and offsets
are never mutated. This is advisory feature extraction, not authorization.

Rules record all matching IDs. Distinct matching targets defer to the model;
an existing override disables the automatic rule result. Rules and taxonomy
versions must agree. Runtime.classification_batch preserves input order and uses
the existing explicit bounded pool, in waves limited by max_documents; every
document has its own runtime budget and a shared cancellation token.
The bridge releases the GIL and contains panics. Python's optional native facade
returns a domain record, and unavailable native support fails explicitly.

Acceptance: bounded multilingual spans/token counts, rule provenance, conflicts,
override preservation, unknown OCR/language, cancellation, resource limits and
ordered multi-document execution. Production routing remains disabled pending
the existing baseline and per-format adoption gates.
