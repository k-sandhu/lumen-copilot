/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_API_BASE_URL: string;
  readonly VITE_WS_BASE_URL: string;
  // Developer-only pages (/docs, /features) gate (issue #40). OFF by default;
  // "true"/"1" turns them on (still auth-gated). NOT read via import.meta.env in
  // app code — it is resolved at config time in vite.config.ts and injected as
  // the build-time literal `__DEV_PAGES_ENABLED__` (below) so the dev-page chunks
  // are dead-code-eliminated when OFF. Declared here only for tooling/.env typing.
  readonly VITE_ENABLE_DEV_PAGES?: string;
  // VITE_PROXY_TARGET is build/dev tooling only (see vite.config.ts), not client.
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}

/**
 * Developer-only pages (/docs, /features) build-time gate (issue #40).
 * `define`-injected literal boolean from `vite.config.ts` (derived from
 * `VITE_ENABLE_DEV_PAGES`). Read in `api/env.ts` as a compile-time constant so
 * Rollup can dead-code-eliminate the dev-page chunks + their inlined internal
 * docs when OFF. Always replaced by a literal at build time — never a real
 * runtime global.
 */
declare const __DEV_PAGES_ENABLED__: boolean;
