/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_API_BASE_URL: string;
  readonly VITE_WS_BASE_URL: string;
  // VITE_PROXY_TARGET is build/dev tooling only (see vite.config.ts), not client.
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
