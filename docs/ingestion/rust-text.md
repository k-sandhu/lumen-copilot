# Text/Markdown/code candidate (#680)

The candidate decodes bounded bytes with the foundation's encoding detector,
counts decoding errors, normalizes CRLF/CR to LF, and keeps decoded source-line
locations separate from Unicode renderer offsets. Text paragraphs split on blank
lines. Markdown uses supplied heading/list/code roles; fenced and indented code
remain whole canonical blocks. Source MIME types supply language labels and
render the entire file as one code block. No code executes.

Both NATIVE_TEXT_ENABLED and NATIVE_TEXT_SHADOW default false. Shadow failures
retain the live Python result and emit only safe counts/error categories. Common
code MIME types are admitted only with enabled cutover and an installed wheel.
Binary signatures/control-rich content are rejected even with text MIME types.
Chunking remains the separately tracked #667; this parser preserves code units.

Acceptance: heading hierarchy, lists, intact code, source language, legacy/BOM
decoding diagnostics, line normalization and exact offsets, binary and budget
negatives, independent admission and shadow rollback.

Merge gate: hold until measured against the baseline evaluation; a human merges.
