# Spec 0005 — User preferences, saved searches & search suggestions

> Status: **proposed** (Phase 0 of epic #144). Contract-first (ADR-0006): this spec
> and the matching `contracts/openapi.yaml` additions are frozen **before** the
> backend or frontend write feature code. Grounds itself in the mission filters
> ([AGENTS.md §2](../../AGENTS.md)) and the security & domain invariants
> ([spec 0004](0004-security-and-domain-invariants.md)).

## 1. Why

Three product asks, all user-scoped and cross-device, so they live behind the
contract rather than per-device `localStorage`:

1. **Default model.** A user can choose their default chat model; new chats start
   with it instead of the server-wide default.
2. **Search typeahead.** The search box shows options *as the user types*, powered
   by **server-side** suggestions (so they can surface real, permitted content —
   not just the user's own past queries).
3. **Saved searches.** A user can save a query (with its filters) under a name and
   re-run it later.
4. **Recent search history.** Recent queries are remembered server-side, offered in
   the typeahead, and clearable.

## 2. Decisions (and why)

- **Server-side, not per-device.** Unlike appearance (#134, deliberately per-device
  `localStorage`), these are *account* state the user expects to follow them across
  devices, so they are first-class API resources. (Decided with the product owner.)
- **Suggestions are server-side and permission-trimmed.** Typeahead document
  matches must obey the same allow-set as `/search` (INV-2) — surfacing a document
  title the caller can't open would leak existence. So `suggest` runs through the
  **`retrieval/` chokepoint** (CC-1); there is no unfiltered suggest path.
- **A saved search captures exactly what `/search` accepts** (`query`,
  `collection_id`, `source`, `type`). Applying one = re-running `/search` with those
  params; no new search semantics are invented.
- **Recent history is recorded as a side-effect of `/search`** (additive behavior —
  no response-shape change), de-duplicated per `(tenant, user, normalized query)`,
  newest-first, capped (server constant, default 20). It is **not** populated by
  `suggest` (typing is not searching).
- **`default_model_id` is validated against the `/models` registry** and stored as
  an *override*; `null` means "use the server default". If a stored model later
  leaves the registry, resolution **fails closed** to the server default rather than
  erroring a chat (a removed model never strands a user). Setting an unknown model
  via `PATCH /preferences` is a **422** (`unknown_model`), reusing the existing
  chat model-validation path.

## 3. Resources & endpoints

Full shapes live in `contracts/openapi.yaml` (the frozen source of truth). Summary:

| Capability | Endpoint(s) | Auth / scope |
|---|---|---|
| Preferences | `GET /preferences`, `PATCH /preferences` | the **caller's** row (no id in path) |
| Saved searches | `GET`/`POST /saved-searches`, `GET`/`PATCH`/`DELETE /saved-searches/{savedSearchId}` | per-user, tenant-scoped |
| Suggestions | `GET /search/suggest?q&limit` | caller's allow-set (permission-trimmed) |
| Recent history | `GET /search/recent?limit`, `DELETE /search/recent` | the caller's history |

Reuses the existing `Problem` error model, `Cursor`/`Limit` parameters, and the
`ResultSource` enum. All additive — no existing shape changes (contracts/AGENTS.md §2).

`UserPreferences { default_model_id: string|null, updated_at }`
`SavedSearch { id, name, query, collection_id?, source?, type?, owner_id, created_at, updated_at }`
`Suggestion { kind: completion|document|saved_search, text, document_id?, source?, saved_search_id? }`
`RecentSearch { query, last_used_at }`

## 4. Data model (backend)

Three new tenant-scoped tables (each `TenantScopedMixin` + `TimestampMixin`, RLS
policy added in the same migration following migration 0007's pattern):

- **`user_preferences`** — one row per user: `user_id` (unique within tenant),
  `default_model` (nullable string). Upserted by `PATCH /preferences`.
- **`saved_searches`** — `owner_id`, `name`, `query`, `collection_id?`, `source?`,
  `type?`. Keyset-paginated like `chat_sessions`.
- **`recent_searches`** — `user_id`, `query`, `last_used_at`; unique
  `(tenant_id, user_id, normalized_query)`; capped per user (oldest evicted).

Suggestions need **no new table**: document matches reuse `documents`/`chunks` +
the `retrieval/` permission filter; `completion`/`saved_search` suggestions read
`recent_searches`/`saved_searches`.

## 5. Security & domain invariants (spec 0004)

- **INV-1 / INV-2 (tenancy & permission).** Every new table is tenant-scoped (RLS
  backstop, migration 0007 pattern) and owner-scoped in the repository; a
  cross-tenant or other-user `GET/PATCH/DELETE` returns **404** (never 403, no
  existence disclosure). `suggest` document matches are filtered by the same
  `AllowSet` as `/search`; a user can **never** see another user's document titles,
  saved searches, recent queries, or preferences.
- **INV-4 (auth).** All endpoints are `bearerAuth`; missing/expired token → **401**.
- **INV-8 (input).** Malformed input → **422** (empty `q` on suggest, unknown
  `default_model_id`, bad cursor, over-long name/query).
- **INV-6 (auditable).** `suggest` is a retrieval surface: like `/search`, it emits
  a retrieval audit event through the one audit sink (CC-8), recording that a
  permission-trimmed suggest ran (count of allowed vs. excluded candidates), so the
  trim is provable after the fact. Preferences/saved-search writes are ordinary
  CRUD and are **not** retrieval events (no audit event required).

## 6. Acceptance criteria

**Preferences**
- AC-P1 `GET /preferences` returns the caller's `default_model_id` (or `null` when
  unset) — a fresh user has `null`.
- AC-P2 `PATCH /preferences { default_model_id }` with a registry id persists it and
  returns the updated row; `null` clears it.
- AC-P3 An unknown `default_model_id` → 422 `unknown_model`; nothing is persisted.
- AC-P4 A new chat session with no explicit model uses the caller's
  `default_model_id` when set, else the server default. A stored model later removed
  from the registry falls back to the server default (no error).

**Saved searches**
- AC-S1 Create / list (newest-first, cursor-paginated) / get / rename-or-edit /
  delete, all scoped to the caller.
- AC-S2 A saved search round-trips `query` + the three optional filters and, applied
  on the client, reproduces the same `/search` call.
- AC-S3 Negative: another user's (same or different tenant) saved search → 404 on
  get/patch/delete; over-long `name`/`query` → 422.

**Suggestions**
- AC-G1 `GET /search/suggest?q=ta` returns ≤ `limit` suggestions ranked by relevance,
  mixing `completion` (from the caller's recents/saved) and `document` (permitted
  title/filename matches).
- AC-G2 Negative (the load-bearing one): a `document` suggestion is returned **only**
  if the caller may open that document — a prefix that matches *another* user's
  document yields no `document` suggestion for it.
- AC-G3 Empty/whitespace `q` → 422; `limit` is bounded (e.g. 1–20).

**Recent history**
- AC-R1 Running `/search` records the (normalized, de-duplicated) query;
  `GET /search/recent` returns them newest-first, capped.
- AC-R2 `DELETE /search/recent` clears the caller's history (204) and is idempotent.
- AC-R3 Recents are per-user — one user's recents never appear for another.

## 7. Frontend behavior (Phase 2)

- **Default model.** The model picker gains a "Set as default" affordance (and/or a
  preferences surface); the chosen default seeds the model for **new** sessions. A
  per-session override still works and persists to the session (unchanged).
- **Search combobox.** The search input becomes an accessible combobox
  (`role=combobox` + listbox, arrow/enter/escape, `aria-activedescendant`): on input
  it **debounces** (~150–200 ms) and calls `suggest`, merging server suggestions
  with the caller's recent + saved searches; choosing a suggestion fills the box
  (and, for a `saved_search`, applies its filters). Every state — idle, loading,
  empty, error — is handled; it degrades to the current submit-to-search if `suggest`
  fails.
- **Saved searches.** A "Save this search" action (name it) and a list to apply or
  delete; applying one sets the query + filters and runs the search.
- **Recent history.** Surfaced in the combobox under a "Recent" group with a clear
  action wired to `DELETE /search/recent`.

All four are **state-complete** and accessible (frontend/AGENTS.md quality bar);
the markdown/citation rules are unaffected.

## 8. Non-goals (this epic)

- No fuzzy/typo-tolerant ranking beyond what `retrieval/` already does; `suggest` is
  prefix/lexical over permitted titles + the caller's own query history.
- No cross-user/shared saved searches or team defaults (per-user only).
- No new connectors or content types; `source`/`type` reuse the frozen enums.
- No admin policy over model defaults (governance stays in the existing admin
  surface).

## 9. Verification

- **Contract (Phase 0):** `openapi.yaml` parses; the generated FE types build; shapes
  are additive (no existing schema changed) and reuse `Problem`/params/enums.
- **Backend (Phase 1):** `ruff` + `mypy --strict` clean; `pytest` green incl. the
  negative tests above (cross-tenant/other-user → 404, unknown model → 422, empty `q`
  → 422, **no cross-user document suggestions**); migrations reversible with the RLS
  lens.
- **Frontend (Phase 2):** `tsc` + ESLint + Vitest (each state) + a Playwright pass on
  the combobox happy path; `vite build` green.

---
*Provenance: product scoping decided in-session with the sponsor (contract-first
backend; all four features; server-side suggestions). Grounds itself in
[spec 0004](0004-security-and-domain-invariants.md) and [ADR-0006](../architecture/0006-contract-first-parallel-implementation.md).*
