# Spec 0007 — Conversation context surfaces

> Status: **proposed** (Phase 0 of feature [#432](https://github.com/k-sandhu/lumen-copilot/issues/432);
> companion to [spec 0006](0006-conversational-chat-affordances.md) / #429 — same
> PR, same contract-first discipline). Grounds itself in
> [spec 0004](0004-security-and-domain-invariants.md), the usage substrate
> (#409, [ADR-0016 §2.6](../architecture/0016-context-engine-and-cache-first-prompting.md))
> and the artifacts platform (CC-B #208).

## 1. Why

Four asks (sponsor, 2026-07-17) that make the conversation's *context*
first-class and visible:

1. **Context-usage meter** (Cursor-style): how many tokens this conversation has
   spent and how full the model's window was last turn.
2. **Context-elements panel**: what the conversation actually touched — the
   documents cited, the tools invoked, the artifacts produced.
3. **Artifacts in chat** (Claude-style): files generated during the conversation
   render inside the app (preview + download), not only on the standalone
   `/artifacts` page.
4. **@-mentions**: typing `@` in the composer opens a permission-trimmed
   document picker; chosen documents pin the next answer's retrieval scope.

## 2. Decisions (and why)

- **The meter reports the assembler's own arithmetic, not an approximation.**
  `GET /chat/sessions/{id}/usage` sums the session's `llm_usage` rows (#409) in
  SQL and computes the input budget via the SAME formula `assemble_context`
  applies (window − output headroom − safety margin, exposed as
  `input_budget_for_model` in `llm/context.py`). Utilization =
  `last.prompt_tokens / input_budget_tokens`, client-derived. `window_known`
  flags the conservative-fallback case honestly. Live turns update the meter
  from `done.data.usage` (already on the wire) without refetching.
- **The context panel aggregates what the wire already carries.** Documents =
  distinct cited sources across loaded messages; tools = the persisted
  `tool_invocations` trace (#377); artifacts = the existing
  `GET /artifacts?session_id=` listing. No new aggregate endpoint — the panel is
  a projection, so it can never disagree with the thread. (Retrieved-but-uncited
  documents are deliberately NOT listed: the audit log is the accessed-data
  surface; the panel shows what the conversation *used*.)
- **Artifacts-in-chat reuses the artifacts feature wholesale.** The chat view
  mounts the existing `ArtifactPanel` scoped by `sessionId` in a side pane (the
  citation-inspector pattern); per-message artifact chips already exist via
  `CodeRunPanel`. No new endpoint, no new viewer.
- **@-mentions pin retrieval, they don't upload.** The picker suggests over the
  caller's *permitted* documents via the existing permission-trimmed
  `GET /search/suggest` (INV-2 by construction — a title the caller can't open
  is never suggested). `SendMessageRequest` gains `document_ids` (≤ 20,
  additive): the runtime threads them into `ToolContext`, `retrieval/` ANDs a
  `document_id` terms filter into BOTH hybrid legs alongside the allow-set
  (narrow-only — an inaccessible id contributes nothing and discloses nothing,
  which is also why ids are NOT validated at send time: a per-id 404 would be an
  existence oracle). The model-visible question gains a count-only pinned note
  (names would need a permission-checked lookup this feature doesn't otherwise
  require; the user already sees names as pills). Pins are composer state,
  sticky until removed, sent with each message while present.
- **Pinning scopes `search_text`** (the grounding path). The by-name/metadata
  tools (`search_documents`, `list_documents`, `get_document`) stay unscoped —
  the model may still look around; the *evidence* search is what the user
  scoped.

## 3. Contract surface (frozen shapes)

- `GET /chat/sessions/{sessionId}/usage` → `SessionUsage { model, totals,
  last?, input_budget_tokens, window_known }` (schemas `SessionUsageTotals`,
  `SessionUsageLast`; 404 on a non-visible session — INV-1/INV-2).
- `SendMessageRequest.document_ids?: uuid[] (maxItems 20)` — additive.
- No WS change; no artifacts/suggest change (reused as-is).

## 4. Security & domain invariants (spec 0004)

- **INV-1/INV-2.** The usage endpoint is owner-gated exactly like the session it
  describes (404, non-disclosure). Document pinning only narrows: the allow-set
  runs in both engine legs AND at Postgres hydration (defense in depth,
  unchanged); the suggest picker is already permission-trimmed. No new
  unfiltered path.
- **INV-6.** No new audit surface: pinned searches audit as ordinary
  `retrieval.query` events; the usage read is not a retrieval.
- **INV-8.** `document_ids` over 20 → 422; malformed uuid → 422 (schema).

## 5. Acceptance criteria

- AC-1 (meter): the chat surface shows conversation token totals and last-turn
  window utilization; it updates live on `done` and survives reload via the
  endpoint; a fresh session shows an honest empty meter.
- AC-2 (panel): a Context pane lists distinct cited documents (click → the
  existing source inspector), the tool trace with outcomes, and the session's
  artifacts; every section handles loading/empty/error.
- AC-3 (artifacts): artifacts produced in the conversation are viewable in-app
  from the chat (preview for image/text/markdown, download otherwise) via the
  existing viewer, scoped to the session.
- AC-4 (@-mentions): typing `@` opens a keyboard-navigable, permission-trimmed
  document picker; selection inserts a pill and removes the `@query` text;
  pinned pills ride subsequent sends as `document_ids`; retrieval for those
  answers returns passages only from pinned documents (within the allow-set).
- AC-N1 (negative): a pinned id outside the caller's allow-set yields no
  passages from it and no error/disclosure; `/usage` for a foreign session →
  404; over-limit pins → 422.
- AC-N2 (negative): suggest failures degrade the picker to "no matches" (no
  wedge); artifacts listing failure shows a retryable error, not a blank pane.

## 6. Non-goals

- No retrieved-but-uncited document listing (audit owns "accessed").
- No cross-session usage analytics (AgentOps #300 owns dashboards).
- No artifact creation path changes; no document upload via @.
- No pin persistence server-side (composer state only, by design — resending is
  explicit).

## 7. Verification

- Backend: totals/last repo math; endpoint 200/404/empty; budget formula parity
  with the assembler; `_hybrid_body` carries the `document_id` terms filter in
  BOTH legs; runtime threads `document_ids` → `ToolContext` → `search_text`;
  pinned note only in the assembled question. `ruff`/`mypy --strict`/`pytest`.
- Frontend: meter states (empty/populated/live-update); panel sections incl.
  error states; @-picker keyboard flow + pill lifecycle; Vitest + `tsc` +
  ESLint; `pnpm gen:api` reconciled.
- Live: compose round-trip — pin a document, ask, observe scoped citations,
  watch the meter move, open the artifacts pane.

---
*Provenance: sponsor ask in-session (2026-07-17). Companion feature issue:
[#432](https://github.com/k-sandhu/lumen-copilot/issues/432); built in the same
PR as #429 (shared surface, shared review).*
