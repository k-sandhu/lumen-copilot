# Conversation evidence and inline citation selection

Tracking: [#436](https://github.com/k-sandhu/lumen-copilot/issues/436).
Depends on complete passages [#611](https://github.com/k-sandhu/lumen-copilot/issues/611).

Tools show conversation-scoped S (passage), D (document) and W (web evidence)
handles beside human-readable source names. IDs and offsets remain server-side.
Every use rechecks current permissions and answer scope. Corpus identity records
hold source IDs, spans and a content hash, not source text. Media timestamps,
segment and speaker provenance contribute an additional hash without storing
transcript or speaker plaintext. Changing media provenance changes the passage
identity, so the final permission read rejects the old handle. Public web records
hold validated URLs and the exact visible evidence. Handles survive compaction
independently of the prompt, are never reassigned and die with the conversation.
Atomic reservations may leave numbering gaps after an aborted answer.
Reservations commit through the coordinator's session before any tool writes,
then restore the transaction-local tenant binding. Serial and denial-only paths
do not acquire a second database connection. Preview reservations remain inside
the preview's rollback boundary.

The final model answer cites `[S1]` or `[W1]` inline. Only permitted, currently
visible cited evidence is persisted. D handles are navigation only. Unknown,
forbidden and navigation markers are stripped, with any speculative answer
retracted and corrected before persistence/terminal delivery. A generated refusal
without source markers and deterministic fallback both have zero citations.
Compaction retains labeled visible evidence rather than merely its source ID.
Recalled conversation prose is navigation, never source evidence.
If selected corpus evidence is revoked, changes, or leaves the answer scope
before final persistence, the complete speculative answer is retracted and
replaced with the honest zero-citation fallback. Removing its marker alone
would retain prose derived from forbidden evidence. The final permission read
emits a count-only evidence-rehydration audit event in the answer transaction.
Multiple revisions of one chunk retain distinct handle candidates during the
answer. A cited current revision is persisted with its matching handle; citing
an old revision, or mixing old and current revisions, triggers the same complete
answer retraction. Chunk-level deduplication must not discard a later handle.

Corpus REST/WS citations add optional `handle`; legacy citations retain numbered
sources. The handle coexists with media timestamp, transcript-segment and speaker
fields through persistence, reload and WebSocket delivery. Redacted citations
retain only their nondisclosing shell, without media provenance. Chat and run-detail citations use that same shape and preserve the
handle on reload. Public web evidence is a separate additive REST `Message.web_citations`
array and `event:web_citation` payload, with no invented document or chunk IDs.
`done.citationCount` counts both types. Both event types arrive after final
resolution and before the terminal; streaming and reloaded handle links resolve
to the same persisted identity. Unknown events remain forward compatible.
Audited reads and refusals commit their transactions; route tests verify events
in an independent read-back transaction without manually committing the route.

Verification covers used-only citations, unknown handles, refusal zero citations,
revocation/tenant/conversation isolation, durable allocation gaps, compaction,
round-trip reload and safe accessible links. No phrase-matching refusal detector
is introduced; supported premise corrections may cite their supported claims.
