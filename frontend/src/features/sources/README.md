# features/sources — connector grid + sync health (#27)

The `/sources` surface (ADR-0009, the 6th wireframe surface,
`docs/wireframes/sources.html`). A grid of connector cards — one per connected
source — each showing a glyph, name/URL, a sync-health **StatusDot**, an indexed
count, the owner/permission (**PermissionPill**), and the last-synced
(**FreshnessPill**). An **Add source** flow pastes a public URL (the `web`
connector — zero source-side setup), and each card can re-sync or be removed.

## Boundaries

- **Consumes, never edits** the typed `@/api` boundary (`listSources`,
  `createSource`, `syncSource`, `deleteSource` — frozen contract
  `contracts/openapi.yaml §sources`, ADR-0004) and the `@/ui` design-system kit
  (`StatusDot`, `FreshnessPill`, `PermissionPill`, `KpiCard`, `Icon`). No
  transport lives here.
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
  SourcesPanel.tsx        → KPIs + connector grid + add/sync/remove; every state
  SourceCard.tsx          → one connector card (glyph, status, stats, actions)
  AddSourceModal.tsx      → paste-a-URL flow → POST /sources; 422 inline error
  (confirm-before-remove uses the shared components/ConfirmDialog)
model/
  queries.ts              → TanStack Query over api/sources (+ sync polling)
  presentation.ts         → wire Source → status tone/label, glyph, freshness;
                            client URL validation + create-error mapping
  types.ts                → re-exports the frozen wire types + kit StatusTone
```

## States (frontend/AGENTS.md "every state, not just success")

loading (KPI + card skeleton grid) · empty ("Add your first source — paste a
link") · error with an actionable retry · the 401 re-auth dead-end messaged
without a pointless retry (spec 0004 INV-4) · success grid · the new source shows
as `pending`, and the list polls while anything is `pending`/`syncing` so the
transition to `ready`/`error` surfaces.

## Contract / trust notes

- **Add** is owner-gated tier-T1 (ADR-0009 §5). The server runs the authoritative
  **SSRF** check (ADR-0009 §3); an invalid OR blocked URL comes back as **422**
  (INV-8) — `code: url_blocked` for a block — and renders INLINE in the modal.
  The client URL validation is a UX guard only, never a security boundary.
- **Sync / remove** are owner-gated; a non-owner / cross-tenant source returns
  **404** (existence non-disclosure, INV-1/INV-2). Remove cascades the source's
  ingested documents, so it gates behind an explicit confirm.
- Ingested content is owner-scoped within the tenant — surfaced as the
  "Owner only" PermissionPill (mission filter #1, permissioned by default).
