# Search — permission-trimmed cited search (`features/search`)

The frontend search slice (issue #84, M2 wave 1), built against the **frozen**
`/search` contract (`contracts/openapi.yaml` §search, #80) via the typed
`api/search.ts` boundary and the W0d trust-signal kit (`@/ui`, #81). It builds in
parallel with the backend (ADR-0006): conform to the contract, mock the responses
in dev and tests, so a contract match is an integration match.

This slice **consumes, never edits**, `frontend/src/ui/**` (the trust kit) and
`frontend/src/api/**` (the only backend caller). The app shell (issue #110) owns
the chrome (brand + top bar + nav rail + theme + account) and the auth gate (the
layout route's `RouteGuard`); the screen reuses the shared shell-aware `PageChrome`
plus `ScrollArea` / `ErrorBoundary` / the markdown pipeline — it re-scaffolds none
of them and renders **no** chrome of its own (no duplicate top bar inside the shell).

## Where the pieces live

Feature (`features/search`):

- `model/queries.ts` — `useSearch`, a TanStack Query hook over the `search` api/
  boundary. **Disabled until `q` is non-empty**, so the contract's
  422-on-empty-`q` path is never fired just from mounting. Results are server
  state (cached, never mirrored into a store).
- `model/presentation.ts` — pure wire→prop mappings: `toPassageRuns` (snippet +
  `match_spans` → `<mark>` runs, with the invariant that runs tile the snippet
  exactly once), `freshnessLabel` / `isStale` (relative recency), permission and
  source-glyph maps, `trimNotice` (the hidden-count disclosure copy), plus the
  filter/citation helpers added in **#118**: `sourceLabel` / `sourceFacets`
  (the frozen `ResultSource` enum, tallied from the data) and `typeFacets`
  (content types DERIVED from the data), and `segmentAnswer` / `hasInlineCitations`
  (splitting answer text into plain runs and resolvable `[n]` citation markers).
- `model/queries.ts` — `useSearch` plus `useSearchCollections` (#118), which loads
  the caller's collections (`GET /collections`) to back the collection scope filter.
- `components/SearchComposer.tsx` — the type-to-query input; a real `<form>` so
  Enter and the button submit the trimmed query; empty queries never submit.
- `components/SearchFilters.tsx` (#118) — the LEFT filter sidebar of the two-column
  layout. Every facet is backed by the REAL `/search` contract + data: a
  **collection** scope (`collection_id`), a **source** scope (the frozen
  `ResultSource` enum — uploaded docs / chat messages / connected sources), and a
  **content-type** scope (`type`) whose facet values are derived from the `type`
  strings the server actually returned. No invented connectors
  (Slack/Jira/Tickets/Code/People). All three facets drive the **server** query, so
  `results`, `direct_answer` and `hidden_count` stay coherent (see "Filters" below).
- `components/DirectAnswerBlock.tsx` — the optional cited direct answer, rendered
  through the sanitizing markdown pipeline (never raw). Polished in **#118**: a
  "Cited" badge + an "Evidence: N sources" badge, a "Permission-checked" pill and
  a "Freshest source …" pill, and **INLINE `[n]` citation markers** placed within
  the answer text (each a clickable `CitationChip`) rather than a separate chip
  row; when the answer carries no inline markers it falls back to a trailing
  "Sources" chip row so it is still navigable. Each marker opens a `SourceInspector`
  on the cited passage. A citation whose `result_id` is absent from `results` (or an
  out-of-range `[n]`) is **dropped / left literal** — the UI refuses to render an
  un-retrievable (un-verifiable) citation (spec 0004 INV-3).
- `components/SearchResultRow.tsx` — one ranked result built from the kit: source
  glyph, `<mark>`-highlighted snippet, why-it-matched rationale, owner,
  `FreshnessPill` (amber when stale), `PermissionPill`.
- `components/TrimNotice.tsx` — "N results hidden — you don't have access" from
  `hidden_count`; renders nothing when nothing was hidden.
- `components/SearchScreen.tsx` — the feature root; composes a page head + the
  pinned composer + the TWO-COLUMN body (filter sidebar left, answer + results
  right, #118) and implements **every** state: initial / loading / error
  (actionable retry) / empty / success. The composer is pinned; the results pane
  scrolls independently.
- `components/SearchPage.tsx` — the `/search` screen body. The app shell (#110)
  owns the chrome and the layout route owns the auth gate, so this just nests the
  `SearchScreen` through the shared shell-aware `PageChrome` (like Audit/Admin) —
  no bespoke header, back-link, theme toggle, account menu, or nested `RouteGuard`.
  Kept INSIDE the slice so the route is self-contained (ADR-0008 §1 — a feature
  edits only its own files).
- `route.tsx` (`/search`, lazy) + `nav.ts` — auto-discovered via
  `import.meta.glob` (ADR-0008 §3); no edit to `routes/router.tsx`.

## Invariants honored (spec 0004)

- **INV-2 Permission trim:** results contain only permitted passages; withheld
  results are disclosed via `hidden_count` (the TrimNotice) without leaking their
  content. The wire never returns a fully-hidden result, so the result rows only
  ever show `allowed` / `restricted` (content-withheld) permission states.
- **INV-3 Citation integrity:** the direct answer's citations must reference a
  passage present in `results`; an un-resolvable citation is dropped rather than
  rendered.
- **INV-4 Authn / INV-8 Input:** a 401 surfaces as "session expired" with retry; a
  422 surfaces as "rephrase". Both ride the bearer + silent-refresh wiring in the
  `api/` client.

## Filters — backed by the real contract only (#118)

The `/search` contract supports exactly three narrowing params (`collection_id`,
`source`, `type`) plus paging. The sidebar honors that envelope and nothing more —
**all three are sent as server query params** so the response stays internally
coherent:

- **Collection**, **source** AND **content type** are sent to `/search`. The server
  re-runs retrieval over the narrowed, permission-trimmed set and re-derives the
  `direct_answer` and `hidden_count` against exactly the `results` it returns.
- **Why the type facet is server-side, not client-side (the INV-3 fix):** a client
  -only type filter narrows the rendered rows but leaves `direct_answer` and
  `hidden_count` untouched — so selecting a type that hides a row the answer cites
  would leave a **visible answer with dropped / literal citations** (an uncited
  answer — spec 0004 INV-3, #118). Routing `type` through the server keeps the
  answer's citations always resolvable to a present, visible row.
- The type facet's _values_ are still derived from the data (never a hardcoded list
  the backend can't serve), so a faceted type can never imply content the MVP
  doesn't index. As with the source facet, selecting a type collapses the facet to
  that type; the always-present "Any type" reset row clears it.

The wireframe's connector list (Slack / Jira / Tickets / GitHub / Salesforce /
People / Code) is intentionally **not** reproduced — those source kinds aren't in
the `ResultSource` enum and the MVP backend can't serve them.

## Out of scope (#84 / #118)

Pagination UI (the contract exposes `next_cursor`; an infinite/"load more" affordance
lands when result volume warrants it), and click-through navigation into a result's
source document (the `document_id` deep link lands with the documents-viewer wiring).

## Wiring up with the live BE

Contract-true today against mocks (`*.test.tsx` use a mocked fetch). At BE
integration (#83), confirm: `GET /search?q=…` returns `SearchResponse` with
`results[]` (permission-trimmed), an optional `direct_answer` whose `citations[]`
reference present `result_id`s, and `hidden_count`; empty `q` → 422; missing token
→ 401.
