# Context generation audit contract proposal — #686

ADR-0006 Phase 0 review requested before changing the closed API enum.
Add `document.context_requested` and `document.context_generated` to the existing
`AuditEventType` enum (GET /audit type filter and audit response type). No endpoint,
request body, authentication, permission rule, pagination or WS shape changes.

Requested: a system actor, document resource ID, tenant scope, model, fingerprint
and reserved-token ceiling, committed atomically with the durable claim before
model dispatch. Generated: the same scope, model, fingerprint and output-character
count, committed atomically with the generated cache result. Neither carries
source text, enrichment text or provider secrets. Audit failure blocks dispatch.

Generated context remains disabled by default. Existing event values remain valid.
After owner review, regenerate the client, validate contract/backend enum parity,
and run citation exclusion and audit rollback tests before operational generation
is enabled. The draft PR preserves deterministic context while this is reviewed.
