# Documents — collections, upload & viewer (`features/documents`)

The frontend documents slice (issues #49 and #571), built against the **frozen**
document/media contract and honoring specs 0004 and 0008
(security & domain invariants). It builds in parallel with the backend (ADR-0006):
conform to the contract, mock the responses in dev and tests.

This slice STACKS on the #48 auth foundation — it reuses the `api/` client (bearer +
silent refresh), the auth `RouteGuard`, the app chrome, and the shared UI primitives;
it re-scaffolds none of them.

## Where the pieces live

Transport (the `api/` boundary — the only backend caller):

- [`api/documents.ts`](../../api/documents.ts) — typed `listCollections` /
  `createCollection` / `updateCollection` / `deleteCollection`, `listDocuments` /
  `getDocument` / `deleteDocument`, signed preview/download capability creation,
  paginated transcript reads, and extracted text.
- [`api/documentUploads.ts`](../../api/documentUploads.ts) — the `/api/v2` metadata-only
  multipart control plane plus direct storage PUTs. The shared manager bounds three
  files and four parts per file, uploads `File.slice()` bodies with no bearer/cookies,
  aggregates progress, retries/re-signs with jitter, resumes verified completed
  parts, and aborts provider sessions when cancelled before the non-cancellable
  completion boundary. An interrupted completion is reconciled idempotently.

Feature (`features/documents`):

- `model/queries.ts` — TanStack Query hooks for collections and documents.
  `useDocuments` **polls** (2.5 s) while any document is still ingesting
  (pending|processing) and goes quiet once everything is settled — so the
  pending→processing→ready→failed transitions surface without hammering the backend.
  Mutations invalidate the relevant keys.
- `model/uploadStore.ts` — ephemeral, client-side per-file upload state (Zustand):
  queued/preparing/uploading/finalizing progress + transient inline errors. It keeps
  the selected `File` only in memory so an error can resume in-session. The durable record is the `Document`
  (server state), which is NOT mirrored here (frontend/AGENTS.md).
- `model/useUploadDocuments.ts` — bridges the shared multipart manager to the store;
  maps typed failures, cancellation, fresh restart, and resumable retry.
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
- `components/DocumentUpload.tsx` — document/audio/video drag-drop + picker, bounded
  queues, aggregate progress, phase labels, pre-finalization cancel,
  resume/start-again, and typed
  per-file outcomes.
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
  `SourceInspector`), then uses the shared signed-access viewer: PDFs render in an
  unsandboxed iframe; audio/video use native `preload="metadata"` players above a
  paginated diarized transcript; office and text/markdown use extracted text. Media
  timestamps seek after metadata without autoplay, and an expired playback URL is
  refreshed once while preserving time/play state. A
  non-ready document explains it has no preview yet and skips the content fetch.
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
- **INV-4 Authn:** control-plane calls ride bearer + silent refresh. Signed storage
  PUT/GET requests deliberately carry neither Lumen authorization nor cookies.

## Out of scope (#49)

Sharing UI, connectors, and in-document highlight of cited passages — the last lands
with the chat citations UI, not here.

## Wiring up with the live BE

Contract-true against focused tests. Live integration must confirm multipart CORS
exposes ETag without credentials, completion returns one pending `Document`, signed
media GETs support byte ranges, and transcript cursors/timestamps stay player-relative.
