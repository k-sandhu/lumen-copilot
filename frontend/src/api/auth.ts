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
import { request, registerRefreshHandler } from './client';
import { setAccessToken, clearAccessToken } from './token';
import type { CurrentUser, LoginRequest, TokenResponse } from './types';

/** Exchange email + password for an access token (AC-1). Stores the token. */
export async function login(body: LoginRequest, signal?: AbortSignal): Promise<TokenResponse> {
  const token = await request<TokenResponse>('/auth/login', {
    method: 'POST',
    json: body,
    skipAuth: true,
    signal,
  });
  setAccessToken(token.access_token);
  return token;
}

/** Mint a new access token from the refresh cookie (AC-2). Stores the token. */
export async function refresh(): Promise<TokenResponse> {
  const token = await request<TokenResponse>('/auth/refresh', {
    method: 'POST',
    skipAuth: true,
  });
  setAccessToken(token.access_token);
  return token;
}

/** The authenticated principal (AC-2). */
export function getCurrentUser(signal?: AbortSignal): Promise<CurrentUser> {
  return request<CurrentUser>('/auth/me', { signal });
}

/**
 * Revoke the session server-side and clear local state (AC-2). The in-memory
 * token is cleared even if the network call fails, so the client always ends up
 * logged out locally — never wedged in a half-authenticated state.
 */
export async function logout(): Promise<void> {
  try {
    await request<void>('/auth/logout', { method: 'POST' });
  } catch {
    // Best-effort revocation; local logout proceeds regardless.
  } finally {
    clearAccessToken();
  }
}

/**
 * Register the silent-refresh implementation with the client. Idempotent; call
 * once at app boot. Kept here (not in client.ts) so the client stays free of the
 * auth import cycle. A failed refresh clears the token so the guard routes to
 * login.
 */
export function installAuthRefresh(): void {
  registerRefreshHandler(async () => {
    try {
      await refresh();
    } catch (error) {
      clearAccessToken();
      throw error;
    }
  });
}
