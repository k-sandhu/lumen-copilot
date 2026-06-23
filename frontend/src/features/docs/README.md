# Developer pages — docs viewer + features catalog

Two developer-facing pages in the SPA. They are **excluded from the app-shell nav
rail** (issue #110) — the shell's `routes/shell/navModel.ts` maps only the product
surfaces into the Workspace/Administration groups, so `/docs` and `/features` are
reachable by URL but never listed. They render inside the shared app shell like any
other authenticated route (the previous floating `NavOverlay` was removed in #110),
and are lazy-loaded.

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

## Gating — build-time eliminable (issue #40)
These pages surface internal docs/research, so they are gated behind
`VITE_ENABLE_DEV_PAGES` (**OFF by default**, see `.env.example`). The gate is
**build-time**, not merely runtime — the security property is "the bytes are not
shipped", because static JS is fetchable by direct URL regardless of auth or a
nav gate, and the docs viewer inlines every repo markdown file into its chunk.

- `vite.config.ts` resolves the flag at config time and injects it as the literal
  `__DEV_PAGES_ENABLED__` (via `define`). Read in `api/env.ts` as `DEV_PAGES_ENABLED`.
- Each feature's `route.tsx`/`nav.ts` gates its `lazy(() => import(...))` on that
  **literal**, with the `import()` held inside the gated branch. When OFF, Rollup
  dead-code-eliminates the whole branch — so the DocsPage/FeaturesPage chunks and
  the inlined docs are **not emitted at all**, and the modules export
  `route`/`navItem` = `undefined`, which `routes/discovery.ts` drops (so `/docs`
  and `/features` are absent from the nav and unroutable → 404).
- When ON, each page sits behind the shell's auth `RouteGuard` like `/documents`,
  so it still requires an authenticated session.
- `src/buildguards/dist-no-dev-pages.test.ts` runs a real flag-OFF `vite build` and
  greps the output for internal-doc strings + dev-page markers — it fails if the
  gate ever regresses to a runtime read (e.g. dynamic `import.meta.env[key]`),
  which Vite cannot inline and Rollup cannot tree-shake.
