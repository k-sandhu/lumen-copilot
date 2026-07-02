# 11. Assistant & agent-runtime — configured single-agent chat

- **Status:** Accepted *(sponsor-delegated decision; records the design that closes the [#202](https://github.com/k-sandhu/lumen-copilot/issues/202) spike so the E1 features become Ready)*
- **Date:** 2026-07-02
- **Builds on:** [ADR-0003](0003-application-stack.md) (stack), [ADR-0004](0004-architecture-boundaries-and-adapters.md) (module boundaries — `api/ → services/ → domain/` + adapters; one owner per concern), [ADR-0006](0006-contract-first-parallel-implementation.md) (contract-first, then parallel build), [ADR-0008](0008-conflict-free-parallel-delivery.md) (vertical slices, serialized seams, auto-discovery), [ADR-0010](0010-dedicated-text-search-engine.md) (the retrieval store behind the `retrieval/` seam), [spec 0004](../specs/0004-security-and-domain-invariants.md) (INV-1 tenancy, INV-2 owner-or-grant, INV-3 citations, INV-6 audit, INV-7 read-before-write, §2.5 risk tiers)
- **Epic:** [#197](https://github.com/k-sandhu/lumen-copilot/issues/197) (Custom Assistants & Agent Builder). Coordinates with the CC-A tool platform [#207](https://github.com/k-sandhu/lumen-copilot/issues/207).

## Context

The product already has everything a "custom assistant" *runs* on. `backend/app/services/chat_runtime.py` is the grounded answer path: per turn it injects a **system prompt**, advertises a **tool set** (`chat_tools.TOOL_SPECS` — today the three retrieval tools), and retrieves over a **knowledge scope** (the per-send `collection_ids`), with the INV-1/INV-2 permission filter enforced **inside** `retrieval/` (the single chokepoint) and citations (INV-3) built only from passages the tools actually returned. `chat_sessions` already carries a per-session `model` (`ChatSession.model`), so a chat can already run on a chosen model.

An **assistant** is therefore not a new engine. It is a **saved, named, governed version of exactly those three inputs** — instructions, tool allow-list, knowledge scope — plus a model default, an owner/backup, and an autonomy level. Starting a chat "from an assistant" means loading that saved config and feeding it into the runtime the chat already uses. The new work is the **data model + CRUD + versioning + ownership + the builder/library UI**; the runtime barely changes.

This ADR pins:

1. the **data model** (`assistants`, immutable `assistant_versions`, and two nullable FKs on `chat_sessions`);
2. the **runtime-reuse** wiring (how a session with an assistant injects instructions → system prompt, `tool_allowlist` → the CC-A allowed-tool set, `knowledge_scope` → the retrieval filter) and why ad-hoc chat stays the default;
3. the **autonomy enum** and what each level means *mechanically now*, given the MVP is T0/T1;
4. **ownership** rules (owner + backup to publish, audited transfer, orphan handling);
5. the **`/assistants` contract sketch** to hand to the [#210](https://github.com/k-sandhu/lumen-copilot/issues/210) contract-freeze;
6. the **scope boundary** — Skills and the full workflow engine are explicitly **out of v1**, each with a reason so their features close cleanly.

Two decisions are load-bearing on the invariants and are non-negotiable in every dependent feature:

- **An assistant's `knowledge_scope` may only *narrow*, never widen, what its runner can retrieve (INV-2).** The per-user permission filter still runs at retrieval time, keyed off the *running* principal — an assistant scope is an *additional* filter, not a grant. An assistant can never surface a passage its runner could not already retrieve; a shared assistant does not share its owner's documents.
- **An assistant's autonomy level may not exceed the risk tier of the tools it is allowed** ([spec 0004 §2.5](../specs/0004-security-and-domain-invariants.md)). At MVP the whole product is T0/T1, so autonomy governs only app-local tools through the CC-A approval seam; T2+ external actions stay out of scope.

## Decision

### 1. Data model — `assistants` + immutable `assistant_versions`; nullable FKs on `chat_sessions`

Two new tenant/owner-scoped tables in `backend/app/db/` (models + repositories; the owning module per [ADR-0004 §6](0004-architecture-boundaries-and-adapters.md)). Both are `TenantScopedMixin` (non-null `tenant_id`, FK → `tenants.id`, INV-1) and inherit the fail-closed RLS backstop already applied to every tenant-scoped table ([spec 0004 §2.1](../specs/0004-security-and-domain-invariants.md), migration `0007`).

**`assistants`** — the *mutable head* / working definition:

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | |
| `tenant_id` | uuid, non-null, FK `tenants.id`, indexed | INV-1 |
| `owner_id` | uuid, non-null, FK `users.id` | accountable owner (E6-8) |
| `backup_owner_id` | uuid, **nullable**, FK `users.id` | required to be set **before publish** (see §4), not at draft time |
| `name` | text, non-null | |
| `description` | text, nullable | |
| `instructions` | text, nullable | the system-prompt text injected at run time |
| `model` | text, **nullable** | a model id from the `/models` registry; `NULL` ⇒ the smart server default, resolved fail-closed at run time exactly like `user_preferences.default_model` |
| `knowledge_scope` | jsonb, non-null, default `{}` | `{ collection_ids: uuid[], source_ids: uuid[], modes: (company\|uploaded\|web\|model)[] }` — the retrieval filter (a *narrowing* set, §2) |
| `tool_allowlist` | jsonb (string[]), non-null, default `[]` | tool names the run may use; the CC-A allowed-tool set. `[]` ⇒ the ad-hoc default set (the three retrieval tools) until CC-A lands, then the registry default |
| `autonomy_level` | enum, non-null, default `suggest` | `suggest \| draft \| act_with_approval \| act_auto` (§3) |
| `status` | enum, non-null, default `draft` | `draft \| published \| disabled` |
| `created_at`, `updated_at` | timestamptz | `TimestampMixin` |

**`assistant_versions`** — *immutable* published snapshots (E6-7 versioning + rollback):

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | this is the row a run/session pins to |
| `tenant_id` | uuid, non-null, FK, indexed | INV-1 |
| `assistant_id` | uuid, non-null, FK `assistants.id` `ON DELETE CASCADE` | |
| `version` | int, non-null | monotonic per assistant; `UNIQUE (tenant_id, assistant_id, version)` |
| `author_id` | uuid, non-null, FK `users.id` | who published this version |
| `config` | jsonb, non-null | the **full frozen** definition (name, description, instructions, model, knowledge_scope, tool_allowlist, autonomy_level) at publish time |
| `notes` | text, nullable | release note the author supplies |
| `diff_summary` | text, nullable | human-readable delta from the prior version (computed at publish; advisory) |
| `created_at` | timestamptz | |

Rows in `assistant_versions` are **write-once** (append-only): the application DB role holds `INSERT`/`SELECT` but **no `UPDATE`/`DELETE`** on it, so a published version is a durable, auditable snapshot (same posture as `audit_events`, [spec 0004 §2.4](../specs/0004-security-and-domain-invariants.md)). **Publish** = validate (owner + backup present, §4) → freeze the current `assistants` row into a new `assistant_versions` row (`version = max+1`) → set `assistants.status = published`. **Rollback** = create a *new* version whose `config` copies a prior version's `config` (never mutate or delete history) and re-point the head; the audit trail shows the rollback as its own event.

**`chat_sessions` gains two nullable FKs** (additive migration; ad-hoc sessions leave both `NULL`):

- `assistant_id` uuid, **nullable**, FK `assistants.id` `ON DELETE SET NULL`;
- `assistant_version_id` uuid, **nullable**, FK `assistant_versions.id` `ON DELETE SET NULL`.

A session started from an assistant records **both**: `assistant_id` (which assistant) and `assistant_version_id` (the exact pinned snapshot it ran, so the transcript is reproducible even after the assistant is edited or rolled back). Ad-hoc chat leaves both `NULL` and behaves **exactly** as today — this is the default and unchanged path. The same two nullable FKs are the natural attachment point for the later headless **run record** ([#235](https://github.com/k-sandhu/lumen-copilot/issues/235)), which is out of scope here.

**Sharing** uses the existing `grants` pattern ([spec 0004 §2.2](../specs/0004-security-and-domain-invariants.md)), not a new mechanism: an assistant is owner-scoped and deny-by-default; a grant (`resource_type` extended to include `assistant`) lets other principals in the tenant *see and start* it. Crucially, a grant on the **assistant** conveys the right to *run* it — it does **not** convey access to the owner's documents; §2 still holds because retrieval re-filters per the running user.

### 2. Runtime wiring — reuse `chat_runtime.py`, inject three inputs, keep ad-hoc as default

No new runtime. The plan confirmed by this ADR:

- A `POST /chat/sessions` with an optional `assistantId` (§5) resolves the assistant, **pins its current published version** (`assistant_version_id`), and stamps both FKs on the new `chat_sessions` row. A session with **no** `assistantId` is ad-hoc and unchanged.
- When the answer runtime starts for a session that has an `assistant_version_id`, a thin **assembler in `services/`** (not in the runtime, not in `api/`) loads the pinned version's `config` and derives three inputs the runtime already consumes:
  - **`instructions` → system prompt.** The version's `instructions` replace (or, per the impl's decision, prepend to) `GROUNDED_SYSTEM_PROMPT` — the grounding/citation contract (INV-3) is preserved; instructions add persona/role, they never remove the grounding rules.
  - **`tool_allowlist` → the CC-A allowed-tool set.** The runtime hands the allowed-tool set to `run_tool`; a call to a tool **outside** the set returns a typed `tool_not_permitted` tool *result* (the model sees it; the run continues), enforced in **one** place per [#207 AC-2](https://github.com/k-sandhu/lumen-copilot/issues/207). Until CC-A lands, `tool_allowlist` narrows `TOOL_SPECS`; after it lands, it selects from the registry.
  - **`knowledge_scope` → the retrieval filter.** `collection_ids`/`source_ids`/`modes` are passed as the retrieval scope (the seam the runtime already has for `collection_ids`). This is an **additional narrowing filter layered on top of** the per-user INV-2 permission predicate inside `retrieval/` — never a replacement for it.
- **Per-user permission still runs at retrieval time.** The permission filter in `retrieval/` is keyed off the **running principal**, exactly as for ad-hoc chat. The assistant scope can only *intersect* (narrow) the set of passages that principal could already retrieve. This is the INV-2 guarantee restated: **an assistant cannot grant access its runner lacks.** A shared assistant run by user B retrieves only what **B** may retrieve, scoped further by the assistant — never what the owner A may retrieve. Negative tests are mandatory (INV-2 category): a run whose scope names a collection the runner cannot access returns nothing from it; a shared assistant never leaks the owner's private documents.
- **Audit (INV-6) is unchanged and additive:** the runtime already emits `retrieval.query` + `answer.generated`; an assistant-backed turn records the `assistant_id`/`assistant_version_id` in the event metadata so the trail shows which assistant/version produced the answer.

Because the injection points already exist, the runtime change is small and localized to the `services/` assembler + threading the allowed-tool set and scope through the existing parameters. No `llm/`, `retrieval/`, `realtime/`, or contract-envelope change is required for the run path.

### 3. Autonomy levels — `suggest | draft | act_with_approval | act_auto`

The enum (E7-7, [#218](https://github.com/k-sandhu/lumen-copilot/issues/218)):

| Level | Meaning | Mechanical effect **at MVP** (T0/T1 only) |
|---|---|---|
| `suggest` | Proposes; the user acts. | Read-only. May use **T0** tools (retrieval). Any **T1** side-effecting app-local tool (e.g. file-write, code-run) is **not** executed — it would surface as a suggestion/approval request, never an auto-action. Default for new assistants. |
| `draft` | Produces a draft artifact; the user commits it. | As `suggest` — still may not *execute* a T1 side-effecting tool without stepping up; "draft" is about output shape, not autonomy to act. |
| `act_with_approval` | May take app-local actions **after explicit approval**. | Routes **T1** side-effecting tools through the **CC-A approval seam** ([#207 §3](https://github.com/k-sandhu/lumen-copilot/issues/207)): the tool blocks on an approval-request event until approved/denied. Unapproved ⇒ not executed (INV-7-style). |
| `act_auto` | May take app-local actions **without** per-action approval. | Executes **T1** app-local tools automatically, **still audited** (INV-6). No approval prompt for T1; the tier ceiling still applies. |

**Non-negotiable bounds** (both are negative-tested, [#218 AC-1/AC-N](https://github.com/k-sandhu/lumen-copilot/issues/218)):

- **The tier ceiling.** `autonomy_level` may not exceed the highest tier in `tool_allowlist` ([spec 0004 §2.5](../specs/0004-security-and-domain-invariants.md)). Since the MVP ships only T0/T1 tools, autonomy governs **only T0/T1** here. **T2+ external actions remain entirely out of scope** ([spec 0004 §2.5](../specs/0004-security-and-domain-invariants.md) — "no code path performs a T2+ action"); no autonomy level unlocks them in v1.
- **Admin caps.** A tenant policy ceilings the maximum autonomy an assistant may be **published**/upgraded to (globally, or per tool/department/user-group — the exact granularity is [#218](https://github.com/k-sandhu/lumen-copilot/issues/218)'s to implement). Publishing above the cap is **rejected and audited**. Cap changes are audited. The enforcement lives in `services/` (the publish use-case) and again in CC-A's invoke path (defense in depth), not in the router.

### 4. Ownership — owner + backup to publish; audited transfer; orphan handling

(E6-8, governed by [#217](https://github.com/k-sandhu/lumen-copilot/issues/217).)

- Every assistant has a non-null **`owner_id`**. A **`backup_owner_id`** is **required before an assistant may be published** (draft may exist without one; publish validates its presence — an illegal publish without a backup is a **409**, INV-8). Owner and backup must be distinct users in the same tenant.
- **Ownership transfer is audited.** Reassigning `owner_id` (or `backup_owner_id`) emits an audit event (additive `AuditAction`, [spec 0004 §2.4](../specs/0004-security-and-domain-invariants.md)); only the current owner or a tenant `admin` may transfer.
- **Orphaned assistants** — owner (and backup) deprovisioned — are **admin-reassigned or disabled**, never left runnable without an accountable owner. The FKs use `ON DELETE` guards that keep the row (they do not cascade-delete the assistant with the user); an assistant with no valid owner is surfaced to admins for reassignment and may be moved to `disabled`. A `disabled` assistant cannot start new sessions.

### 5. Contract surface — `/assistants` REST (hand to [#210](https://github.com/k-sandhu/lumen-copilot/issues/210)); no new WS envelope

Additive to `contracts/openapi.yaml`, reusing the RFC-9457 Problem + pagination shapes ([ADR-0006](0006-contract-first-parallel-implementation.md)). This is a **sketch for the [#210](https://github.com/k-sandhu/lumen-copilot/issues/210) contract-freeze**, not the frozen contract itself:

- `GET /assistants` — list (paginated; the caller's own + assistants granted to them).
- `POST /assistants` — create a **draft** (`AssistantCreate`).
- `GET /assistants/{id}` · `PATCH /assistants/{id}` (`AssistantUpdate`) · `DELETE /assistants/{id}`.
- `POST /assistants/{id}/publish` — draft → published; **requires owner + backup present** (409 otherwise) and honors the admin autonomy cap (§3).
- `GET /assistants/{id}/versions` — the immutable version list.
- `POST /assistants/{id}/rollback` — body `{ version }`; creates a new head version from that snapshot (§1).
- `POST /chat/sessions` **gains an optional `assistantId`** — **additive** to the existing shape; existing clients are unaffected; a session with no `assistantId` is ad-hoc.

Schemas: `Assistant`, `AssistantCreate`, `AssistantUpdate`, `AssistantVersion`, with the fields from §1 (`name, description, instructions, model, knowledgeScope{collectionIds, sourceIds, modes}, toolAllowlist, autonomyLevel, owner, backupOwner, status`). **Negative contract cases** the freeze must specify: cross-tenant / non-owned / non-granted assistant id → **404** (INV-1/INV-2, existence non-disclosure); malformed create body → **422** (INV-8); publish without a backup owner or above the autonomy cap → **409/403**.

**No new WebSocket envelope.** Assistant-backed chat reuses the **existing** chat stream (`start` → `delta`/`event:tool_call`/`event:tool_result`/`event:citation` → terminal `done`/`error`). The runtime already emits everything the UI needs; the only additive surface (if the trace wants to show *which* assistant/version answered) is optional metadata on existing envelopes, coordinated with `contracts/` — not a new message type.

### 6. Scope boundary — Skills and the full workflow engine are OUT of v1

- **Skills (E6-4, [#216](https://github.com/k-sandhu/lumen-copilot/issues/216)) — DEFERRED to a later ADR.** v1 stays lean: an assistant carries its **own** instructions + knowledge scope + tool allow-list inline. A reusable **Skill** (a shareable instructions+knowledge+tools bundle that many assistants link, with its own versioning and permission-inheritance) adds a `skills` table, an `assistant_skills` link, and non-trivial **merge/precedence** and **no-widen** semantics (a Skill may only *add* tools/knowledge the linking assistant's owner already may use — INV-2). That is enough surface to deserve its own decision once the v1 assistant model has shipped and we know the real reuse patterns. **[#216](https://github.com/k-sandhu/lumen-copilot/issues/216) closes as *deferred* against this ADR** and stays `needs-adr`; nothing in the v1 schema blocks adding it later (the link table is purely additive).
- **Full workflow graph (E6-3 — branching / variables / conditional steps / per-step model) — OUT of v1.** v1 assistants are **"configured single-agent chat,"** not a flow engine: one agent, one system prompt, one tool loop, one knowledge scope. A branching/looping workflow orchestrator is a materially different execution model (a graph runner, step state, per-step models) and gets its **own ADR if/when the need is demonstrated**. Recording the cut here keeps the E1 features honestly scoped to "a saved config the existing runtime consumes."
- Also out (their own epics, unchanged by this ADR): event-driven triggers/scheduling (E5, [#201](https://github.com/k-sandhu/lumen-copilot/issues/201)) and multi-/sub-agent orchestration beyond a single run (E7-6).

### 7. Boundaries — no new external system, so **no new boundary-table row**

This feature introduces **no new external system**. It reuses the existing owned modules: `db/` (the two new tables + repositories), `services/` (assistant CRUD/publish/version use-cases + the run-config assembler), `retrieval/` (unchanged permission chokepoint; assistant scope is an *additional* narrowing filter it already accepts), `llm/` (unchanged model gateway), and the CC-A tool registry ([#207](https://github.com/k-sandhu/lumen-copilot/issues/207)) for allow-list enforcement. Assistant CRUD is a normal `services/` use-case; the runtime stays the `chat_runtime` seam.

Therefore **[AGENTS.md §6](../../AGENTS.md) needs no new "who owns X" row** — no `A new external system ⇒ a new module + a new row` trigger fires. Skills, *if* accepted later, likewise add only a `db/` table, not an external system, so they would not add a boundary row either. **No AGENTS.md change is requested by this ADR** (and per [AGENTS.md §5](../../AGENTS.md) this ADR does not edit it).

## Consequences

- **The E1 features become Ready.** The frozen data model + runtime-reuse plan let [#210](https://github.com/k-sandhu/lumen-copilot/issues/210) (contract) freeze, then [#211](https://github.com/k-sandhu/lumen-copilot/issues/211) (backend core) and [#212](https://github.com/k-sandhu/lumen-copilot/issues/212) (builder + library UI) build in parallel against it ([ADR-0008](0008-conflict-free-parallel-delivery.md)); [#214](https://github.com/k-sandhu/lumen-copilot/issues/214) (versioning), [#217](https://github.com/k-sandhu/lumen-copilot/issues/217) (ownership/library governance), and [#218](https://github.com/k-sandhu/lumen-copilot/issues/218) (autonomy caps) inherit this schema and enum.
- **The invariants hold by construction.** Because retrieval re-filters per the running user, an assistant's scope can only *narrow*; a shared assistant never leaks its owner's data (INV-2). Because the tier ceiling and admin caps gate publish and invocation, no assistant can act beyond its tools' tier (INV-7); and T2+ stays out of the codebase entirely.
- **Small runtime blast radius.** The change is a `services/` config assembler + threading an allowed-tool set and scope through parameters the runtime already has. Ad-hoc chat is untouched (both FKs `NULL`), so the default path carries zero behavioral risk.
- **Immutable version history is auditable and reproducible.** A session/run pins the exact `assistant_version_id` it ran; editing or rolling back an assistant never rewrites what a past transcript ran on.
- **Cost / risk.** The main correctness risk is the **no-widen** guarantee — an assistant scope must intersect, never bypass, the per-user filter — which is why INV-2 negative tests are mandatory in [#211](https://github.com/k-sandhu/lumen-copilot/issues/211)/[#218](https://github.com/k-sandhu/lumen-copilot/issues/218). Publish/ownership introduce new state transitions (draft→published→disabled, transfer, rollback) that need INV-8 illegal-transition tests. The **CC-A dependency** ([#207](https://github.com/k-sandhu/lumen-copilot/issues/207)) is real: robust `tool_allowlist` + approval-seam enforcement lands with the registry; until then `tool_allowlist` narrows the fixed `TOOL_SPECS` and the approval seam is inert (consistent with the T0/T1-only MVP).

## Follow-ups (named, not silently defaulted)

- **Skills ADR** — decide the `skills`/`assistant_skills` shape, version-pin-vs-follow policy, and merge/precedence + no-widen semantics ([#216](https://github.com/k-sandhu/lumen-copilot/issues/216), deferred here).
- **Workflow-engine ADR** — only if/when branching/variable/conditional workflows are demonstrably needed (E6-3, out here).
- **Headless run records** — the run-side reuse of `assistant_id`/`assistant_version_id` for scheduled/headless runs is [#235](https://github.com/k-sandhu/lumen-copilot/issues/235) / [ADR spike #206](https://github.com/k-sandhu/lumen-copilot/issues/206), not this ADR.
- **`grants` `resource_type` extension to `assistant`** — the concrete migration/enforcement for sharing an assistant is [#217](https://github.com/k-sandhu/lumen-copilot/issues/217)'s to land against [spec 0004 §2.2](../specs/0004-security-and-domain-invariants.md) (the *pattern* is decided here; the schema change is additive and not made in this docs-only ADR).
- **Additive audit actions** — `assistant.published`, `assistant.rolled_back`, `assistant.owner_transferred`, `assistant.autonomy_capped` extend [spec 0004 §2.4](../specs/0004-security-and-domain-invariants.md) when [#211](https://github.com/k-sandhu/lumen-copilot/issues/211)/[#217](https://github.com/k-sandhu/lumen-copilot/issues/217)/[#218](https://github.com/k-sandhu/lumen-copilot/issues/218) land.
