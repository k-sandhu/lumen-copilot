# Notebook candidate (#684)

Acceptance: notebook version 4, supplied Markdown structure, intact code cells,
text outputs and binary placeholders in cell/output order. Zero-based cell and
output indices appear in source regions; source Markdown retains one-based lines.
Language comes only from notebook metadata; unknown remains unknown. Nothing runs.

JSON is preflighted for depth, memory and work before streaming deserialization.
Text output is capped at max_record_bytes on Unicode boundaries with truncation
diagnostics and a partial outcome. Partial cutover is rejected by the shared seam.
Source cells are never silently truncated; aggregate output/deadline limits fail.
NATIVE_NOTEBOOK_ENABLED and NATIVE_NOTEBOOK_SHADOW default false. New upload MIME
acceptance requires enabled cutover and a wheel exposing the notebook capability.

The foundation notebook fidelity fixture contains malformed JSON (follow-up #731).
Its failure remains reported; separate valid generated tests prove extraction.

Merge gate: hold until measured against the baseline evaluation; a human merges.
