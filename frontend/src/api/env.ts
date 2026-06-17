/**
 * The ONLY place runtime env is read for the api/ boundary. Only `VITE_`-prefixed
 * vars reach the client (Vite contract). Values come from `.env` (see
 * `.env.example`): VITE_API_BASE_URL=/api, VITE_WS_BASE_URL=/ws — same-origin
 * paths the Vite dev proxy forwards to the backend.
 */

function readEnv(key: string, fallback: string): string {
  const value = import.meta.env[key as keyof ImportMetaEnv];
  return typeof value === 'string' && value.length > 0 ? value : fallback;
}

/** REST base, e.g. "/api". Same-origin in dev via the Vite proxy. */
export const API_BASE_URL: string = readEnv('VITE_API_BASE_URL', '/api');

/** WebSocket base, e.g. "/ws". Same-origin in dev via the Vite proxy. */
export const WS_BASE_URL: string = readEnv('VITE_WS_BASE_URL', '/ws');
