# Ingestion shadow comparison and per-format routing — #687

Sponsor-directed implementation under ADR-0027, ADR-0025 and #669. Python parsers
remain installed. No API/WS change. Adds internal operator tooling only.

Independent PDF/docx/pptx/xlsx/text/markdown modes default to Python. Shadow runs
the bounded candidate beside Python and always returns the exact Python result;
candidate failure, incompleteness or diagnostic persistence failure cannot change
that result. Native returns only a complete candidate, otherwise fails closed.
Unsupported/unlanded format candidates fail closed, never silently route to Python.
The only candidate adapter landed in this dependency stack is supervised PDFium;
other native format adapters remain in their own issues. No new format is added.

Route and candidate build/budget identity enter the extraction checkpoint key.
Switching one format changes only its checkpoint identity; switching back to Python
recomputes under the baseline identity. Native publication is limited to documents
without existing chunks; existing evidence needs an immutable generation path.

Comparisons retain numeric/code diagnostics only: format, candidate outcome,
evidence lengths, exact equality, positional code-point mismatch count, canonical
block count and safe failure category. Positional mismatch is not edit distance or
an extraction/answer quality metric. Per-document records are tenant-scoped and
administrator-readable only after current document permission checks. Aggregate
reports are grouped by format/status. Diagnostic failure has a safe operational
counter, with no source text or exception detail. New records and their existing
processing-stage audit commit together; no new audit wire enum is introduced.

Operator commands are token-authenticated and tenant-bound. Administrators still
need current document access. Bounded, cursor-paged inventory supports document,
collection and tenant scope. Original-byte replay recomputes comparison from the
existing object-store boundary, never enqueues ordinary destructive ingestion.
Re-ingestion activation/retention/backfill remains a concrete proposal pending
owner review under ADR-0025. The execute-generation option fails before any write
or original-byte read until that policy is frozen. No old citation is rewritten.

Acceptance: shadow failure/incompleteness/storage failure preserve Python;
independent mode/rollback and checkpoint invalidation; native incomplete rejects;
cross-tenant and wrong-role diagnostics denied; current permission revocation;
content-free format reports; original-byte replay leaves chunk/citation IDs and
spans unchanged; generation execute gate; offline RLS/DDL upgrade/downgrade.

Migration 0048 is stacked on #669's 0047. Origin/main is 0046; sibling drafts also
reserve 0047. The owner must sequence/renumber migrations before merging.

Merge gate: hold until measured against the baseline evaluation; a human merges.
