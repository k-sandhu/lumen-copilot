/// <reference types="vitest/config" />
import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';
import { fileURLToPath, URL } from 'node:url';

// --- Developer-only pages build-time gate (issue #40) ---------------------
// The /docs and /features pages render internal docs/ADRs/specs (the docs
// viewer inlines EVERY repo markdown file via import.meta.glob). They must be
// *build-time eliminable*: when the flag is OFF, the dev-page chunks — and the
// inlined internal docs they carry — must not be emitted at all, regardless of
// auth (static JS assets are fetchable by direct URL). A runtime gate alone is
// not enough; the bytes still ship.
//
// We inject the flag as a literal boolean via `define`, so the feature gates
// read a true compile-time constant (`__DEV_PAGES_ENABLED__`). Rollup then
// dead-code-eliminates each `false ? lazy(() => import('./DocsPage')) :
// undefined` branch AND its `import()`, dropping the chunk + inlined docs.
// (A dynamic `import.meta.env[key]` read — the prior bug — is opaque to the
// minifier, so nothing was eliminated.)
//
// Truthy ("true"/"1", case-insensitive) ⇒ on; anything else / unset ⇒ off.
function parseDevPagesFlag(raw: string | undefined): boolean {
  const v = (raw ?? '').trim().toLowerCase();
  return v === 'true' || v === '1';
}

// The browser talks ONLY to the Vite dev server; the SPA calls the backend
// same-origin (VITE_API_BASE_URL=/api, VITE_WS_BASE_URL=/ws). This proxy
// forwards those same-origin paths to the FastAPI service over the compose
// network. See docker-compose.yml + .env.example + ADR-0005.
//
// `backend` resolves on the compose network. Override with VITE_PROXY_TARGET
// when running the dev server outside compose (e.g. against localhost:47181).
const proxyTarget = process.env.VITE_PROXY_TARGET ?? 'http://backend:8000';

// Repo root (parent of frontend/). The docs viewer bundles markdown from `docs/`
// and the top-level AGENTS/README contracts via `import.meta.glob('?raw')`; those
// live above the Vite root, so the dev server must be allowed to read them.
// (Production `vite build` inlines them regardless of this allow-list.)
const repoRoot = fileURLToPath(new URL('..', import.meta.url));

export default defineConfig(({ mode }) => {
  // Read VITE_-prefixed env from .env files + process.env for this mode, so the
  // dev-pages flag resolves at CONFIG time and can be inlined as a literal.
  // An explicit `process.env` (e.g. `VITE_ENABLE_DEV_PAGES=true pnpm build`, or
  // CI) wins over a `.env` file — the command-line override is the more explicit
  // intent and keeps the build deterministic regardless of a stray local `.env`.
  const env = loadEnv(mode, repoRoot, 'VITE_');
  const rawDevPagesFlag = process.env.VITE_ENABLE_DEV_PAGES ?? env.VITE_ENABLE_DEV_PAGES;
  // Real builds (`vite build`, dev server): OFF unless explicitly enabled.
  // Vitest (`mode === 'test'`): default ON so the auto-discovery + dev-page
  // suites exercise the ON path without a committed `.env.test` (env files are
  // git-ignored). `vite build` never runs in 'test' mode, so this never relaxes
  // the production default; the OFF build is proven by `dist-no-dev-pages.test.ts`.
  const devPagesEnabled =
    mode === 'test' && rawDevPagesFlag === undefined ? true : parseDevPagesFlag(rawDevPagesFlag);

  return {
    plugins: [react()],
    // Inlined as a literal boolean wherever `__DEV_PAGES_ENABLED__` appears, so
    // Rollup can statically drop the dev-page branches + their chunks when OFF.
    define: {
      __DEV_PAGES_ENABLED__: JSON.stringify(devPagesEnabled),
    },
    resolve: {
      alias: {
        '@': fileURLToPath(new URL('./src', import.meta.url)),
      },
    },
    server: {
      host: '0.0.0.0',
      port: 5173,
      // Allow reading the bundled docs that live above the Vite root (see repoRoot).
      fs: { allow: [repoRoot] },
      // Polling makes file watching reliable across the Docker bind-mount on
      // Windows/macOS hosts.
      watch: { usePolling: true },
      proxy: {
        '/health': { target: proxyTarget, changeOrigin: true },
        '/api': { target: proxyTarget, changeOrigin: true },
        '/ws': { target: proxyTarget, changeOrigin: true, ws: true },
      },
    },
    preview: {
      host: '0.0.0.0',
      port: 5173,
    },
    test: {
      globals: true,
      environment: 'jsdom',
      setupFiles: ['./src/test/setup.ts'],
      css: false,
      // Vitest owns unit/component tests; Playwright owns e2e under tests/e2e.
      exclude: ['node_modules', 'dist', 'tests/e2e/**'],
    },
  };
});
