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

export type TokenChangeReason = 'clear' | 'login' | 'refresh' | 'replace';
type TokenListener = (token: string | null, reason: TokenChangeReason) => void;
type PrincipalReplacementReason = Exclude<TokenChangeReason, 'clear' | 'refresh'>;

let accessToken: string | null = null;
let principalGeneration = 0;
const listeners = new Set<TokenListener>();

/** The current access token, or null when unauthenticated. */
export function getAccessToken(): string | null {
  return accessToken;
}

/** True when an access token is held. */
export function hasAccessToken(): boolean {
  return accessToken !== null;
}

/** Monotonic identity epoch used to reject work completed for an old principal. */
export function getPrincipalGeneration(): number {
  return principalGeneration;
}

/** Replace the principal's held token and notify subscribers. */
export function setAccessToken(
  token: string,
  reason: PrincipalReplacementReason = 'replace',
): void {
  principalGeneration += 1;
  accessToken = token;
  notify(reason);
}

/**
 * Commit a same-principal refresh only while its starting identity epoch is
 * still current. Refreshes deliberately do not advance the principal epoch.
 */
export function setRefreshedAccessToken(token: string, expectedGeneration: number): boolean {
  if (principalGeneration !== expectedGeneration) return false;
  accessToken = token;
  notify('refresh');
  return true;
}

/** Commit a login result only if no logout/switch happened while it was in flight. */
export function setLoginAccessToken(token: string, expectedGeneration: number): boolean {
  if (principalGeneration !== expectedGeneration) return false;
  setAccessToken(token, 'login');
  return true;
}

/** Drop the held token (logout / failed refresh) and notify subscribers. */
export function clearAccessToken(): void {
  principalGeneration += 1;
  if (accessToken === null) return;
  accessToken = null;
  notify('clear');
}

/** Drop a failed refresh's token only if it still belongs to that operation. */
export function clearAccessTokenIfPrincipalUnchanged(expectedGeneration: number): boolean {
  if (principalGeneration !== expectedGeneration) return false;
  clearAccessToken();
  return true;
}

/** Subscribe to token changes. Returns an unsubscribe function. */
export function subscribeToken(listener: TokenListener): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function notify(reason: TokenChangeReason): void {
  for (const listener of listeners) listener(accessToken, reason);
}
