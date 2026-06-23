/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_API_BASE_URL: string;
  readonly VITE_WS_BASE_URL: string;
  // Developer-only pages (/docs, /features) gate (issue #40). OFF by default;
  // "true"/"1" turns them on (still auth-gated). Read via api/env.ts.
  readonly VITE_ENABLE_DEV_PAGES?: string;
  // VITE_PROXY_TARGET is build/dev tooling only (see vite.config.ts), not client.
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
