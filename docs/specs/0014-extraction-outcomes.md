# Spec 0014 — Native extraction outcomes

Tracking: [#624](https://github.com/k-sandhu/lumen-copilot/issues/624), depends on #621.

Keep the existing lifecycle (`pending`, `processing`, `ready`, `failed`) and add
optional nullable `ingestion_outcome`: `empty`, `failed`, `partial`,
`unsupported`, `indexed`. An attempt clears its prior outcome while processing.
Unsupported MIME and unreadable supported bytes are distinct permanent failures.
Transient dependency faults retain retry/backoff; exhausted retries record failed.

No non-whitespace native text means empty, failed lifecycle, zero chunks and an
actionable message. Clear prior chunks and synchronize their deletion. Never
claim that a scanned PDF has searchable text without extracting text. No OCR or
scan classifier is introduced. A PDF with text and at least one native page
whose extracted text is blank is partial; blank pages may be intentional, so the
UI states incomplete native coverage rather than diagnosing an OCR requirement.
Other native formats with text record indexed; that label claims indexed text,
not complete visual/table/image interpretation. Record successful outcomes only
after index synchronization succeeds.

Add optional `Document.searchable`, computed from ready lifecycle and positive
chunk count. It is false for empty extraction, failures and unfinished attempts.
The independent readiness fix makes ready imply search refresh visibility; until
both changes land the pre-existing early-ready window remains a deployment risk.
Frontend status display prioritizes queued/processing lifecycle, then distinguishes
empty, unsupported, failed, partial and indexed; legacy missing metadata retains
existing status labels. Empty extraction never gets a searchable label.

Acceptance: synthetic blank PDF, corrupt native bytes, unsupported MIME, partial
PDF and ordinary text produce distinct persisted outcomes; repeated empty runs
clear old chunks; outcomes survive read-back and are projected without exposing
another tenant or an unauthorized document. Existing view audit commits remain.

No automatic backfill or re-ingestion: existing metadata remains unknown. Re-ingest
retained bytes to measure native coverage; search reindex does not reparse or
infer outcomes. Re-ingestion replaces chunks under current semantics; old citation
offsets are not rewritten. No new dependency or database migration beyond #621.
