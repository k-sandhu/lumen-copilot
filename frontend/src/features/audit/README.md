# features/audit — audit log + provenance drawer (#86)

The `/audit` surface (ADR-0007 IA, M2 Wave 1). An event table of
retrieval / answer / access-decision / action rows with filters and pagination;
clicking a row opens the design-system **ProvenanceDrawer** showing the
per-candidate allow/exclude ledger and the raw event payload (monospace ids).

## Boundaries

- **Consumes, never edits** the typed `@/api` boundary (`listAuditEvents`,
  frozen contract `GET /audit`, ADR-0004) and the `@/ui` design-system kit
  (`AuditRow`, `ProvenanceDrawer`, `Icon`). No transport lives here.
- **Auto-discovered** (ADR-0008 §3): `route.tsx` (`/audit`) + `nav.ts` are picked
  up by `routes/discovery.ts` via `import.meta.glob` — no central file is edited.

## Shape

```
route.tsx                 → /audit (lazy), auto-discovered
nav.ts                    → nav overlay entry, auto-discovered
index.ts                  → slice public surface
components/
  AuditPage.tsx           → RouteGuard + PageChrome shell (auth-gated)
  AuditPanel.tsx          → table + filters + pagination + drawer; every state
  AuditFilters.tsx        → actor / event-type / resource / time-window form
model/
  queries.ts              → TanStack Query over api/audit (cursor pagination)
  presentation.ts         → wire AuditEvent → kit AuditRow / ProvenanceDetail
  filterDraft.ts          → form draft ↔ wire filters (datetime-local → ISO-8601)
```

## States (frontend/AGENTS.md "every state, not just success")

loading (skeleton rows) · empty (filtered vs. genuinely empty) · error with an
actionable retry · the 403/401 access-denied dead-end messaged without a
pointless retry (spec 0004 INV-5 / INV-4) · success table · selected-row drawer.

## Trust invariant

`toProvenanceDetail` preserves **excluded** candidates, so a permission trim is
provable after the fact (mission filter #4 / spec 0004 §2.4).
