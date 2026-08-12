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

import { clearActiveAuthSlotIfMatches, getActiveAuthSlot, setActiveAuthSlot } from './authSlot';

export type TokenChangeReason = 'clear' | 'login' | 'refresh' | 'replace';
type TokenListener = (token: string | null, reason: TokenChangeReason) => void;
type PrincipalReplacementReason = Exclude<TokenChangeReason, 'clear' | 'refresh'>;

let accessToken: string | null = null;
let accessTokenAuthSlot: string | null = null;
let principalGeneration = 0;
let authIntentGeneration = 0;
const listeners = new Set<TokenListener>();

/** The current access token, or null when unauthenticated. */
export function getAccessToken(): string | null {
  return accessToken;
}

/** Auth-slot bound to the live access token, if this is a slot-aware session. */
export function getAccessTokenAuthSlot(): string | null {
  return accessTokenAuthSlot;
}

/** Slot used by a live token, or the persisted selection during page bootstrap. */
export function getRefreshAuthSlot(): string | null {
  return accessTokenAuthSlot ?? getActiveAuthSlot();
}

/** True when an access token is held. */
export function hasAccessToken(): boolean {
  return accessToken !== null;
}

/** Monotonic identity epoch used to reject work completed for an old principal. */
export function getPrincipalGeneration(): number {
  return principalGeneration;
}

/** Monotonic ordering for login/logout/bootstrap intent, independent of identity. */
export function getAuthIntentGeneration(): number {
  return authIntentGeneration;
}

/** Reserve authority synchronously before an auth operation awaits anything. */
export function reserveAuthIntent(): number {
  authIntentGeneration += 1;
  return authIntentGeneration;
}

export function isAuthIntentCurrent(expectedGeneration: number): boolean {
  return authIntentGeneration === expectedGeneration;
}

/** Replace the principal's held token and notify subscribers. */
export function setAccessToken(
  token: string,
  reason: PrincipalReplacementReason = 'replace',
  authSlot: string | null = null,
): void {
  const outgoingSlot = accessTokenAuthSlot ?? getActiveAuthSlot();
  reserveAuthIntent();
  principalGeneration += 1;
  accessToken = token;
  accessTokenAuthSlot = authSlot;
  if (authSlot) setActiveAuthSlot(authSlot);
  else clearActiveAuthSlotIfMatches(outgoingSlot);
  notify(reason);
}

/**
 * Commit a same-principal refresh only while its starting identity epoch is
 * still current. Refreshes deliberately do not advance the principal epoch.
 */
export function setRefreshedAccessToken(
  token: string,
  expectedGeneration: number,
  expectedAuthIntent = authIntentGeneration,
  authSlot: string | null = accessTokenAuthSlot,
): boolean {
  if (principalGeneration !== expectedGeneration || authIntentGeneration !== expectedAuthIntent) {
    return false;
  }
  accessToken = token;
  accessTokenAuthSlot = authSlot;
  notify('refresh');
  return true;
}

/** Commit only the latest login intent; response timing never chooses principal. */
export function setLoginAccessToken(
  token: string,
  authSlot: string,
  expectedAuthIntent: number,
): boolean {
  if (authIntentGeneration !== expectedAuthIntent) return false;
  principalGeneration += 1;
  authIntentGeneration += 1;
  accessToken = token;
  accessTokenAuthSlot = authSlot;
  setActiveAuthSlot(authSlot);
  notify('login');
  return true;
}

/** Drop the held token (logout / failed refresh) and notify subscribers. */
export function clearAccessToken(): void {
  const outgoingSlot = accessTokenAuthSlot ?? getActiveAuthSlot();
  reserveAuthIntent();
  principalGeneration += 1;
  const hadToken = accessToken !== null;
  accessToken = null;
  accessTokenAuthSlot = null;
  clearActiveAuthSlotIfMatches(outgoingSlot);
  if (hadToken) notify('clear');
}

/** Drop a failed refresh's token only if it still belongs to that operation. */
export function clearAccessTokenIfPrincipalUnchanged(
  expectedGeneration: number,
  expectedAuthIntent = authIntentGeneration,
): boolean {
  if (principalGeneration !== expectedGeneration || authIntentGeneration !== expectedAuthIntent) {
    return false;
  }
  if (accessToken === null) {
    const outgoingSlot = accessTokenAuthSlot ?? getActiveAuthSlot();
    accessTokenAuthSlot = null;
    clearActiveAuthSlotIfMatches(outgoingSlot);
    return true;
  }
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
