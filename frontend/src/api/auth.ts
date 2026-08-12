/**
 * Typed auth calls — part of the api/ boundary. Conforms to the frozen
 * contract (contracts/openapi.yaml §auth) and spec 0004 §2.3 (app-managed
 * identity: short-lived access JWT + rotating httpOnly refresh cookie).
 *
 *   POST /auth/login   email+password → TokenResponse (sets refresh cookie)
 *   POST /auth/refresh refresh cookie → TokenResponse (silent re-auth)
 *   GET  /auth/me       → CurrentUser
 *   POST /auth/logout   → 204 (revokes the refresh token / clears the cookie)
 *
 * `login`/`refresh` use `skipAuth` so they carry no stale bearer and never
 * trigger the 401-refresh-retry loop. On success the access token is stored in
 * the in-memory holder (token.ts); the client bearers it onto every subsequent
 * request. The refresh handler is registered with the client so a 401 anywhere
 * triggers exactly one silent refresh + retry (AC-2/AC-4).
 */
import {
  awaitLogoutTransition,
  cancelInFlightRefresh,
  request,
  registerRefreshHandler,
  runLogoutTransition,
} from './client';
import {
  clearAccessToken,
  clearAccessTokenIfPrincipalUnchanged,
  getAccessToken,
  getPrincipalGeneration,
  setLoginAccessToken,
  setRefreshedAccessToken,
} from './token';
import type { CurrentUser, LoginRequest, TokenResponse } from './types';

/** Exchange email + password for an access token (AC-1). Stores the token. */
export async function login(body: LoginRequest, signal?: AbortSignal): Promise<TokenResponse> {
  // Invalidate the old identity before waiting for its coordinated refresh to
  // settle. This prevents that refresh from committing/retrying, and orders any
  // best-effort Set-Cookie it may already have produced before B's login response.
  clearAccessToken();
  await cancelInFlightRefresh();
  await awaitLogoutTransition();
  const principalGeneration = getPrincipalGeneration();
  const token = await request<TokenResponse>('/auth/login', {
    method: 'POST',
    json: body,
    skipAuth: true,
    signal,
  });
  if (!setLoginAccessToken(token.access_token, principalGeneration)) {
    throw new Error('Login result discarded after a principal transition');
  }
  return token;
}

/** Mint a new access token from the refresh cookie (AC-2). Stores the token. */
export async function refresh(
  signal?: AbortSignal,
  principalGeneration = getPrincipalGeneration(),
): Promise<TokenResponse> {
  const token = await request<TokenResponse>('/auth/refresh', {
    method: 'POST',
    skipAuth: true,
    signal,
  });
  if (!setRefreshedAccessToken(token.access_token, principalGeneration)) {
    throw new Error('Refresh result discarded after a principal transition');
  }
  return token;
}

/** The authenticated principal (AC-2). */
export function getCurrentUser(signal?: AbortSignal): Promise<CurrentUser> {
  return request<CurrentUser>('/auth/me', { signal });
}

/**
 * Clear the in-memory token at intent and then attempt server-side revocation
 * (AC-2) with the captured outgoing bearer. The request is best-effort: a late
 * success/failure cannot clear or restore a later principal's token.
 */
export function logout(
  bearer: string | null = getAccessToken(),
  /** The UI may already have performed the synchronous canonical teardown. */
  clearLocalToken = true,
): Promise<void> {
  // Local logout happens at intent, before this best-effort request. Capture the
  // outgoing principal first so clearing the live holder cannot accidentally
  // send a later principal's bearer if revocation is delayed.
  if (clearLocalToken) clearAccessToken();
  return (async () => {
    await cancelInFlightRefresh();
    await runLogoutTransition((signal) =>
      request<void>('/auth/logout', {
        method: 'POST',
        skipAuth: true,
        headers: bearer ? { Authorization: `Bearer ${bearer}` } : undefined,
        signal,
      }).catch(() => {
        // Best-effort revocation; local logout proceeds regardless.
      }),
    );
  })();
}

/**
 * Register the silent-refresh implementation with the client. Idempotent; call
 * once at app boot. Kept here (not in client.ts) so the client stays free of the
 * auth import cycle. A failed refresh clears the token so the guard routes to
 * login.
 */
export function installAuthRefresh(): void {
  registerRefreshHandler(async ({ signal, principalGeneration }) => {
    try {
      await refresh(signal, principalGeneration);
    } catch (error) {
      clearAccessTokenIfPrincipalUnchanged(principalGeneration);
      throw error;
    }
  });
}
