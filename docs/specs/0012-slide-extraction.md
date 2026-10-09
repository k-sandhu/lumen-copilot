# Spec 0012 — Slide structure in extracted text

Tracking: [#617](https://github.com/k-sandhu/lumen-copilot/issues/617).

Content-bearing slides render a one-based `Slide 1: <source title>` heading
(without a title suffix when none exists). Walk shapes in presentation order,
recursively walking grouped shapes. Retain paragraph text and explicit breaks,
including leading, intervening and trailing empty paragraphs within a
content-bearing text frame, whether ordinary or grouped.
Titles are source-derived; no image/chart descriptions or facts are generated.

Tables render `[Table]` / `[/Table]` markers and numbered rows with stable `C1`,
`C2`, … columns, including blanks. The first row supplies header labels as an
explicit heuristic; later values repeat those source labels and any units.
Merged-origin text occurs at its anchor; spanned positions remain blank even
when the underlying covered cell contains text.
Speaker notes render after shapes under `Notes (Slide 1):`. Only an existing
notes slide's body text is read; slide-number/image placeholders are excluded
and accessing extraction never creates notes on slides without notes.

Acceptance fixtures cover multiple slide titles, recursively grouped text,
tables and merged/blank cells, notes present/absent and paragraph breaks.
Content-bearing status comes only from non-whitespace source text, non-spanned
table cells or notes, never from generated headings, table markers or row/column
labels. Slides containing only empty or whitespace-only tables therefore
produce no text evidence or chunks. Corrupt PPTX bytes raise
`DocumentParseError`; all library imports remain inside the helper.

These deterministic source labels improve retrieval context and keep offsets
exact into the rendered string. Future ingestion alone adopts the rendering.
Existing documents require ingestion from retained bytes or a new upload;
search reindex alone copies old chunks. Ingestion replaces chunk IDs under
existing behavior; old citation offsets are never rewritten or retargeted.

Deferred: chart data, figure/caption relationships, visual reasoning, layout
reconstruction, OCR and structure-aware chunking. Notes are distinguished from
visible slide text, but a long table row can still cross a character boundary.
