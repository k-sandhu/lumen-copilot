# Spec 0016 — Permissioned administrator extraction inspection

Tracking: [#628](https://github.com/k-sandhu/lumen-copilot/issues/628), depends on #621.

Record diagnostics beside exact extraction text/maps, including empty text:
character count, replacement characters, suspicious controls excluding ordinary
tab/newline/carriage return, native source-part kind/count, parts with text and
one-based blank part numbers. A blank native part is not diagnosed as a scan.
Nonzero replacement/control counts are inspection warnings, not a quality gate.

Probe DOCX/PPTX native tables and XLSX sheet grids behind lazy parser helpers.
Record probe type, table/sheet-region count, nonempty cell count and count of
cell strings absent from the rendered text after whitespace normalization.
Formula strings without a cached value count as source content for this probe.
This is a conservative text-presence heuristic: it cannot establish correct
row/column association, geometry or visual completeness. A matching string
elsewhere can mask a missing cell. PDF table coverage and unsupported probes are
explicitly unknown, never zero missing cells as a claim of completeness.

`extraction_diagnostics` is an optional nullable Document wire field. The service
first enforces existing document visibility, then projects diagnostics only for
principals with the administrator role. A role is not a visibility override:
another tenant or unauthorized document still returns 404. Members receive null
diagnostics. Preserve the existing committed document-view/list audit behavior.
Administrator diagnostic reads emit `document.viewed` events committed before
the response. The document inspector renders counts, blank parts and qualified warnings only
when the permissioned API supplies them, with no raw native cell text in metadata.

Compute diagnostics after native extraction and persist before chunk/embedding
work so extraction inspection survives later model/index failure. These describe
the latest extraction attempt, not activation of new chunks. Retained source text,
source map and replacement chunks still change atomically after embeddings, so
failed attempts do not retarget existing exact citation slices. A new attempt
replaces prior diagnostics; permanent parse failure records no invented coverage.
Malformed bytes remain typed parse errors. No OCR, automatic retry/profile choice
or new quality gate. No additional dependency or migration beyond #621.

Acceptance: synthetic PDF blank page, decoded replacement/control text, omitted
native DOCX/PPTX table cells and sparse workbook retain measured diagnostics;
empty and ordinary text are distinguished; corrupt bytes raise typed errors;
tenant/permission/member negatives hide diagnostics; administrators can inspect
stored values after ingestion read-back, including a later embedding fault.

Legacy diagnostics remain unknown until original bytes are re-ingested. Search
reindex does not reparse. Existing chunks and historical citation offsets are
never rewritten by inspection. Future structured parser evaluation must compare
these heuristic diagnostics with labeled content coverage rather than using
absence of warnings as an accuracy score.
