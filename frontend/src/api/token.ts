/**
 * In-memory access-token holder — part of the api/ boundary.
 *
 * SECURITY (spec 0004 §2.3, least-exposure): the short-lived access JWT lives in
 * a module-scoped variable, NOT localStorage/sessionStorage. That keeps it off
 * disk and out of reach of persistent-storage XSS exfiltration; a page reload
 * drops it and the app re-establishes a session via the httpOnly refresh cookie
 * (`POST /auth/refresh`). The refresh token itself is NEVER seen by JS — it is an
 * httpOnly, server-set cookie (per contracts/openapi.yaml /auth/refresh).
 *
 * The token is wired into every outbound request by `client.ts` (Authorization:
 * Bearer) and into the WS handshake by `ws.ts`. Subscribers (the auth store) are
 * notified on change so the UI can react when a silent refresh fails.
 */

type TokenListener = (token: string | null) => void;

let accessToken: string | null = null;
const listeners = new Set<TokenListener>();

/** The current access token, or null when unauthenticated. */
export function getAccessToken(): string | null {
  return accessToken;
}

/** True when an access token is held. */
export function hasAccessToken(): boolean {
  return accessToken !== null;
}

/** Replace the held token and notify subscribers. */
export function setAccessToken(token: string): void {
  accessToken = token;
  notify();
}

/** Drop the held token (logout / failed refresh) and notify subscribers. */
export function clearAccessToken(): void {
  if (accessToken === null) return;
  accessToken = null;
  notify();
}

/** Subscribe to token changes. Returns an unsubscribe function. */
export function subscribeToken(listener: TokenListener): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function notify(): void {
  for (const listener of listeners) listener(accessToken);
}
