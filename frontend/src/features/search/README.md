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
  source-glyph maps, and `trimNotice` (the hidden-count disclosure copy).
- `components/SearchComposer.tsx` — the type-to-query input; a real `<form>` so
  Enter and the button submit the trimmed query; empty queries never submit.
- `components/DirectAnswerBlock.tsx` — the optional cited direct answer, rendered
  through the sanitizing markdown pipeline (never raw). Each citation is a real
  `CitationChip` that opens a `SourceInspector` on the cited passage. A citation
  whose `result_id` is absent from `results` is **dropped** — the UI refuses to
  render an un-retrievable (un-verifiable) citation (spec 0004 INV-3).
- `components/SearchResultRow.tsx` — one ranked result built from the kit: source
  glyph, `<mark>`-highlighted snippet, why-it-matched rationale, owner,
  `FreshnessPill` (amber when stale), `PermissionPill`.
- `components/TrimNotice.tsx` — "N results hidden — you don't have access" from
  `hidden_count`; renders nothing when nothing was hidden.
- `components/SearchScreen.tsx` — the feature root; composes the above and
  implements **every** state: initial / loading / error (actionable retry) /
  empty / success. The composer is pinned; the results pane scrolls independently.
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

## Out of scope (#84)

Pagination UI (the contract exposes `next_cursor`; an infinite/"load more" affordance
lands when result volume warrants it), and click-through navigation into a result's
source document (the `document_id` deep link lands with the documents-viewer wiring).

## Wiring up with the live BE

Contract-true today against mocks (`*.test.tsx` use a mocked fetch). At BE
integration (#83), confirm: `GET /search?q=…` returns `SearchResponse` with
`results[]` (permission-trimmed), an optional `direct_answer` whose `citations[]`
reference present `result_id`s, and `hidden_count`; empty `q` → 422; missing token
→ 401.
