# Spec 0030 — Session effective knowledge scope

Status: adopted for #581 part 2 by the owner's lane brief, 2026-10-29.
Builds on [ADR-0011](../architecture/0011-assistant-and-agent-runtime.md) and
[spec 0004](0004-security-and-domain-invariants.md).

## Session projection

`ChatSession.effective_scope` is resolved by the server from the session's
**pinned** assistant version. Create, read, list and update return the same
scope, even after the assistant head is edited, republished or rolled back.
The additive contract is in `contracts/openapi.yaml`: assistant and version
IDs, effective modes, collection IDs, source IDs, and per-field adjustability.
Ad-hoc sessions return explicit `null`. Existing deleted/unresolvable pin
fallback remains unchanged; it cannot manufacture a scope from the head.

Effective modes follow the owner's decision:

- An empty/missing legacy modes list means `company` + `uploaded`, plus `web`
  when `web_search` appears in the pinned tool allowlist.
- A nonempty modes list is authoritative. Unknown values are dropped without
  triggering legacy defaults. `model` is always removed, including from a
  model-only list. Results are deduplicated in company/uploaded/web order.
- Collection/source IDs come from the same snapshot. Empty lists mean no
  additional narrowing on that dimension. This scope is an additional filter;
  it conveys no access to a collection or source (INV-2).
- `adjustable.modes`, `adjustable.collection_ids` and `adjustable.source_ids`
  are all `false`. No per-session or per-turn narrowing is introduced.

The session visibility check runs before scope resolution. Missing, other-owner
and cross-tenant direct reads retain identical 404 behavior and durable denial
auditing; foreign sessions never appear in lists (INV-1/INV-2/INV-6). List
projection batches the tenant-scoped pinned-version reads.

## UI acceptance criteria

1. The session query supplies the displayed scope. Neither the assistant head
   nor launch-time client state supplies modes. History reopening and a fresh
   mount show the same Web state.
2. Fixed modes are informational and cannot be toggled. Excluded modes have a
   short explanation. Model is disabled and described as not used because
   answers are always grounded.
3. Loading and error states announce that scope is unavailable, with a Retry
   action for errors. They never claim an ad-hoc default before a successful
   session read. Confirmed ad-hoc sessions retain company/uploaded defaults.
4. Generated types, the wire mirror and backend responses agree with the
   contract. API tests cover pin persistence and non-disclosure; component and
   presentation tests cover all display states.

## Delivery boundary and reversal

This lane projects scope and removes ineffective UI controls. Retrieval/tool
enforcement and answer provenance belong to #581's scope-server lane; they must
use the same effective-modes rule. No migration is needed. Deletion behavior is
the pre-existing nullable-FK/runtime fallback, not new enforcement in this lane.

To reverse the owner's decision, change `services/knowledge_scope.effective_modes`
and the adjustability flags together with a specified, persisted narrowing
request surface and subset validation. Flipping a UI control alone is insufficient.
