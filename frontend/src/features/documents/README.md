# Documents — collections, upload & viewer (`features/documents`)

The frontend documents slice (issue #49), built against the **frozen** `/collections`
and `/documents` contract (`contracts/openapi.yaml` 0.1.0) and honoring spec 0004
(security & domain invariants). It builds in parallel with the backend (ADR-0006):
conform to the contract, mock the responses in dev and tests.

This slice STACKS on the #48 auth foundation — it reuses the `api/` client (bearer +
silent refresh), the auth `RouteGuard`, the app chrome, and the shared UI primitives;
it re-scaffolds none of them.

## Where the pieces live

Transport (the `api/` boundary — the only backend caller):

- [`api/documents.ts`](../../api/documents.ts) — typed `listCollections` /
  `createCollection` / `updateCollection` / `deleteCollection`, `listDocuments` /
  `getDocument` / `deleteDocument`, `uploadDocument` (multipart, **XHR**-based so it
  can report upload progress — `fetch` exposes none), and `resolveDocumentContentUrl`
  (issues a `redirect: 'manual'` GET so it can READ a 302 `Location` presigned URL
  instead of letting the browser opaquely follow it). Types added to
  [`api/types.ts`](../../api/types.ts).

Feature (`features/documents`):

- `model/queries.ts` — TanStack Query hooks for collections and documents.
  `useDocuments` **polls** (2.5 s) while any document is still ingesting
  (pending|processing) and goes quiet once everything is settled — so the
  pending→processing→ready→failed transitions surface without hammering the backend.
  Mutations invalidate the relevant keys.
- `model/uploadStore.ts` — ephemeral, client-side per-file upload state (Zustand):
  in-flight progress + transient inline errors. The durable record is the `Document`
  (server state), which is NOT mirrored here (frontend/AGENTS.md).
- `model/useUploadDocuments.ts` — bridges `uploadDocument` to the store; maps a
  413/415/422/404/network failure to a clear, user-facing message.
- `model/presentation.ts` — pure status→tone/label and byte-formatting helpers,
  plus the #89 trust-signal derivation: `ingestSteps` (the parse → chunk → embed →
  ready pipeline projected from `status` + `chunk_count`), `statusDotTone`, and
  `fileKind`. The #119 table polish adds `fileKindTone` (type-badge family),
  `relativeTime`/`documentFreshness` (the Updated column, from `updated_at`),
  `ownerLabel` ("You" vs. an honest short id — there is NO display-name field on the
  wire), and `visibility` (the Visibility column, derived from the **real** INV-2
  owner-only invariant — the MVP backend carries no Confidential/Team/Org taxonomy,
  so we never fabricate one). No I/O — unit-tested directly.
- `components/CollectionsSidebar.tsx` — list / create / rename / delete (AC-1).
- `components/DocumentUpload.tsx` — drag-drop + picker, concurrent uploads, live
  progress, per-file success/error (AC-2 / AC-4).
- `components/DocumentList.tsx` — per-collection **table** to the documents.html
  wireframe (#119): columns Name + file-type badge, Collection, Visibility (kit
  `PermissionPill`), Owner, Updated (kit `FreshnessPill`), Status — every column
  backed by a real document field. Filters by status / filename `q`, opens the
  viewer on a (ready) row click or keyboard, deletes; a `failed` row shows its error
  inline (AC-3 / AC-4). A non-ready row is not openable (no bytes to preview yet).
  The row is a `role="button"`, so the Delete action stops **both** click and
  keyboard (Enter/Space) propagation — activating Delete never also opens the viewer.
- `components/DocumentViewer.tsx` — a right-side **drawer** (#89 re-skin) that
  surfaces the metadata grid, the parse → chunk → embed → ready **ingestion trace**
  (kit `StatusDot`), and — when opened on a citation — the cited passage (kit
  `SourceInspector`), then resolves `GET /documents/{id}/content` (following a 302)
  and renders the file in a sandboxed iframe (AC-3). A non-ready document explains
  it has no preview yet and skips the content fetch.
- `components/DocumentsPanel.tsx` — the feature root; the `/documents` route
  ([`routes/DocumentsRoute.tsx`](../../routes/DocumentsRoute.tsx)) wraps it in the
  auth guard + app chrome, reachable from the chat shell's Pages overlay.

## Invariants honored (spec 0004)

- **INV-1 Tenancy / INV-2 Permission:** a cross-tenant or not-permitted collection /
  document returns **404** (never 403); the UI surfaces it as "no longer available"
  rather than disclosing existence. Covered in `documents.test.ts` and the component
  tests.
- **INV-8 Input/state:** a malformed create body → **422** surfaces as an inline
  error; over-size → **413** and unsupported type → **415** surface as clear per-file
  upload errors (AC-4).
- **INV-4 Authn:** every call rides the bearer + silent-refresh wiring from #48; the
  XHR upload sets the same `Authorization` header and `withCredentials`.

## Out of scope (#49)

Sharing UI, connectors, and in-document highlight of cited passages — the last lands
with the chat citations UI, not here.

## Wiring up with the live BE

Contract-true today against mocks. At BE integration, confirm: multipart `POST
/documents` accepts `file` + `collection_id` and returns a `Document` at status
`pending`; `GET /documents/{id}/content` 302s to a presigned URL whose CORS allows the
SPA origin (or streams bytes 200 same-origin via the proxy); and the size/type caps
return 413/415 with a `Problem` body.
