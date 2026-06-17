/// <reference types="vitest/config" />
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { fileURLToPath, URL } from 'node:url';

// The browser talks ONLY to the Vite dev server; the SPA calls the backend
// same-origin (VITE_API_BASE_URL=/api, VITE_WS_BASE_URL=/ws). This proxy
// forwards those same-origin paths to the FastAPI service over the compose
// network. See docker-compose.yml + .env.example + ADR-0005.
//
// `backend` resolves on the compose network. Override with VITE_PROXY_TARGET
// when running the dev server outside compose (e.g. against localhost:47181).
const proxyTarget = process.env.VITE_PROXY_TARGET ?? 'http://backend:8000';

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    host: '0.0.0.0',
    port: 5173,
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
});
