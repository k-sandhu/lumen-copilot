# features/sources — connector grid + sync health (#27, #455)

The `/sources` surface (ADR-0009, the 6th wireframe surface,
`docs/wireframes/sources.html`; managed connectors per ADR-0019). A grid of
connector cards — one per connected source — each showing a glyph, name/detail,
a sync-health **StatusDot**, an indexed count, the permission
(**PermissionPill**), and the last-synced (**FreshnessPill**). An **Add
source** flow pastes a public URL (the `web` connector — zero source-side
setup) or, for a **tenant admin**, configures the managed **Google Drive**
connector (My Drive / a folder / a Shared Drive) and hands the browser to
Google's consent screen. Each card can re-sync or be removed; a managed card
adds the ADR-0019 §5 health surface and the **Connect / Reauthorize** actions.

## Boundaries

- **Consumes, never edits** the typed `@/api` boundary (`listSources`,
  `createSource`, `connectSource`, `syncSource`, `deleteSource` — frozen
  contract `contracts/openapi.yaml §sources`, ADR-0004) and the `@/ui`
  design-system kit (`StatusDot`, `FreshnessPill`, `PermissionPill`,
  `KpiCard`, `Icon`). No transport lives here.
- **Auto-discovered** (ADR-0008 §3): `route.tsx` (`/sources`) + `nav.ts` are
  picked up by `routes/discovery.ts` via `import.meta.glob` — no central file is
  edited. Nests through the shell-aware `PageChrome` (like Audit/Admin/Search), so
  the shell owns the chrome and the slice owns only its own files.

## Shape

```
route.tsx                 → /sources (lazy), auto-discovered
nav.ts                    → nav overlay entry, auto-discovered
index.ts                  → slice public surface
components/
  SourcesPage.tsx         → PageChrome + ErrorBoundary nesting
  SourcesPanel.tsx        → KPIs + connector grid + add/sync/connect/remove;
                            the OAuth return banner; every state
  SourceCard.tsx          → one connector card (glyph, status, stats, actions;
                            gdrive: account/ACL health + Connect/Reauthorize)
  AddSourceModal.tsx      → web URL flow AND the admin gdrive config step →
                            create → connect → consent redirect; errors inline
  (confirm-before-remove uses the shared components/ConfirmDialog)
model/
  queries.ts              → TanStack Query over api/sources (+ sync polling;
                            pending_auth deliberately does NOT poll)
  useConnectReturn.ts     → parses ?connect=ok|error&… (the frozen callback
                            contract), cleans the params, refreshes the grid
  browser.ts              → the one full-page-navigation seam (consent URL)
  presentation.ts         → wire Source → status tone/label, glyph, freshness,
                            ACL freshness, scope labels; client URL validation;
                            create/connect error mapping; the closed
                            connect=error reason → message map
  types.ts                → re-exports the frozen wire types + kit StatusTone
```

## States (frontend/AGENTS.md "every state, not just success")

loading (KPI + card skeleton grid) · empty ("Add your first source — paste a
link") · error with an actionable retry · the 401 re-auth dead-end messaged
without a pointless retry (spec 0004 INV-4) · success grid · the new source
shows as `pending` (`web`) or `pending_auth` (`gdrive`, until consent), and the
list polls while anything is `pending`/`syncing` so the transition to
`ready`/`error` surfaces · the OAuth return renders a success or per-reason
error banner and then cleans the query string.

## Contract / trust notes

- **Add (`web`)** is owner-gated tier-T1 (ADR-0009 §5). The server runs the
  authoritative **SSRF** check (ADR-0009 §3); an invalid OR blocked URL comes
  back as **422** (INV-8) — `code: url_blocked` for a block — and renders
  INLINE in the modal. The client URL validation is a UX guard only.
- **Managed (`gdrive`) mutations are tenant-admin-gated at ACTION time**
  (ADR-0019 §1, INV-5): create, connect, reauthorize, sync, delete. The UI
  renders NO managed affordances for a non-admin (add option, Connect,
  Reauthorize, sync/remove on gdrive cards), and a direct 403 surfaces as an
  inline error state — never a blank pane (the #160 lesson).
- **Consent flow:** create (`pending_auth`) → `POST /sources/{id}/connect` →
  browser navigates to `authorization_url` → the backend callback ALWAYS 302s
  back to `/sources` with the frozen params: `?connect=ok&source={id}` or
  `?connect=error&reason={expired|denied|provider_error|failed}` (a closed
  set, each mapped to a human message).
- **gdrive health surface** (ADR-0019 §5, required fields on the wire):
  `connected_account.email`, `acl_synced_at` (stale mirrors DENY at
  retrieval — surfaced as a FreshnessPill), `unmapped_acl_count` (documents
  visible to no one until identities are attested in Admin — the hint links the
  two surfaces), and `reauthorize_required` → the **Reauthorize** action
  (re-runs connect on the same row).
- **Sync / remove** (`web`) are owner-gated; a non-owner / cross-tenant source
  returns **404** (existence non-disclosure, INV-1/INV-2). Remove cascades the
  source's ingested documents, so it gates behind an explicit confirm.
- `web` content is owner-scoped within the tenant ("Owner only" pill); `gdrive`
  content is retrievable ONLY via the fresh mirrored source ACL ("Source
  permissions" pill) — ownership and Lumen grants never widen it (mission
  filter #1, permissioned by default).
