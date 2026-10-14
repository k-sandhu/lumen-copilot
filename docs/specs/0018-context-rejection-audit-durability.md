# Completed read records survive context-budget rejection

Tracking: [#645](https://github.com/k-sandhu/lumen-copilot/issues/645).
Implements spec 0004 INV-6 using the existing audit sink and tenant-scoped
repositories. It does not change the context engine's budget or refusal policy.

When completed tools are followed by `context_too_large`, the runtime snapshots
its own uncommitted audit events and invocation records for the current answer.
It rolls back the answer transaction before opening a new tenant-bound
transaction that restores only those records. Invocation message references are
cleared because no assistant message exists. Original trusted identities,
timestamps, dispatch ordinals and argument hashes remain intact. Source text and
raw arguments are not copied into this record.

If the owner deletes the chat session between rollback and recovery, completed
read events and invocation records still survive. Recovery resolves session
parents within the requesting tenant, clears missing or foreign-tenant session
references, and holds key-share locks on surviving parents until commit so a
concurrent deletion cannot invalidate the invocation insert. A deletion after
recovery uses the existing `ON DELETE SET NULL` foreign key. The audit sink also
preserves explicit trusted event identities from the canonical sink contract;
independently durable events are never queued for recovery.

Ordinary successful answers keep their existing atomic commit. Independently
committed approval events are excluded from restoration. Preview runs keep their
rollback-only behavior. Restoring read records must not commit staged document,
artifact, assistant-message or citation writes; foreign-tenant records fail
before persistence. Audit restoration failure cannot become a successful answer.

Acceptance tests use the chat route, observe one successful read followed by the
typed refusal with no oversized provider request, and read committed events and
detached invocations from an independent transaction. A staged unrelated write
must disappear. This change covers context-budget rejection; provider errors,
cancellation and crash recovery remain separate work.

The owner-deletion regression enables foreign-key enforcement and commits the
real owner DELETE at the rollback/recovery boundary. Independent read-back must
find all three original events and the original invocation with both session
and message references detached, and no failed-turn writes. Repository coverage
also checks tenant-scoped parent resolution and the production key-share query.
