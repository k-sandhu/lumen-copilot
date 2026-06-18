# Developer pages — docs viewer + features catalog

Two standalone, developer-facing pages in the SPA, linked from the main app by the
floating `NavOverlay` ("🧭 Pages", reveals on hover/focus). They're separate
top-level routes (not nested under the chat shell) and lazy-loaded.

## `/docs/*` — documentation viewer (`features/docs`)

The SPA is **static** (ADR-0003) — there's no server to read `docs/`. So
[`content.ts`](./content.ts) bundles the markdown at **build time** via
`import.meta.glob('?raw')` over `../../../../docs/**/*.md` plus the top-level
agent-contract + repo-readme files. `frontend/vite.config.ts` adds the repo root
to `server.fs.allow` so the **dev** server can read those sibling files; `vite
build` inlines them regardless.

- The pure transforms — slug/group/title derivation and link resolution — live in
  [`model.ts`](./model.ts) and are unit-tested (`model.test.ts`, `content.test.ts`).
- Doc-to-doc links navigate **in-app**: `resolveDocLink` maps relative and
  `docs/`-prefixed links to `/docs/<slug>`, and returns null for external/unknown
  links (which then render as a normal new-tab `<a>`). It's wired through the single
  `MarkdownView` pipeline via its optional `resolveInternalLink` prop — the chat
  renderer's behavior is unchanged.

To add docs: drop any `*.md` under `docs/` and it's picked up automatically. New
top-level contract/readme files must be added to the explicit list in `content.ts`.

## `/features` — features catalog (`features/feature-catalog`)

[`catalog.ts`](../feature-catalog/catalog.ts) is a **curated, typed** list of what
has shipped (a static SPA can't query GitHub at runtime), rendered as status-badged
cards grouped by area. Each entry links to the ADR/spec/PR it came from.

### Maintenance rule
When a feature lands, **add or flip its entry in `catalog.ts` in the same PR**
(AGENTS.md §7.6, living docs). Every internal `/docs/…` link in the catalog must
resolve to a real bundled doc — `catalog.test.ts` fails otherwise, so a
renamed/removed doc is caught instead of shipping a dead link.

## Note on exposure
These pages currently surface internal docs/research to anyone who can reach the
SPA, with no auth (OD-4 is open). The routes + overlay are isolated so they can be
gated behind auth or a build flag once the security invariants land.
