/**
 * Typed auth calls — part of the api/ boundary. Conforms to the frozen
 * contract (contracts/openapi.yaml §auth) and spec 0004 §2.3 (app-managed
 * identity: short-lived access JWT + rotating httpOnly refresh cookie).
 *
 *   POST /auth/login   email+password → TokenResponse (sets refresh cookie)
 *   POST /auth/refresh refresh cookie → TokenResponse (silent re-auth)
 *   GET  /auth/me       → CurrentUser
 *   POST /auth/logout   → 204 (revokes the selected refresh-token family)
 *
 * `login`/`refresh` use `skipAuth` so they carry no stale bearer and never
 * trigger the 401-refresh-retry loop. On success the access token is stored in
 * the in-memory holder (token.ts); the client bearers it onto every subsequent
 * request. The refresh handler is registered with the client so a 401 anywhere
 * triggers exactly one silent refresh + retry (AC-2/AC-4).
 */
import {
  ApiError,
  awaitLogoutTransition,
  cancelInFlightRefresh,
  request,
  registerRefreshHandler,
  runCoordinatedRefresh,
  runLogoutTransition,
} from './client';
import {
  clearAccessToken,
  clearAccessTokenIfPrincipalUnchanged,
  getAccessToken,
  getAccessTokenAuthSlot,
  isAuthIntentCurrent,
  reserveAuthIntent,
  setLoginAccessToken,
  setRefreshedAccessToken,
} from './token';
import { authSlotHeaders, createAuthSlot, subscribeActiveAuthSlot } from './authSlot';
import type { CurrentUser, LoginRequest, TokenResponse } from './types';

/** Exchange email + password for an access token (AC-1). Stores the token. */
export async function login(body: LoginRequest, signal?: AbortSignal): Promise<TokenResponse> {
  // Reserve latest-intent authority and a distinct cookie slot synchronously,
  // before any barrier or transport await. A failed direct login therefore does
  // not surprise-log-out an already authenticated principal, while older login
  // responses can neither commit a token nor overwrite the newer cookie slot.
  const authIntentGeneration = reserveAuthIntent();
  const authSlot = createAuthSlot();
  const outgoing = {
    bearer: getAccessToken(),
    authSlot: getAccessTokenAuthSlot(),
  };
  const attempt: LoginAttempt = { authIntentGeneration, authSlot, cancelled: false };

  // An already-cancelled caller has not dispatched credentials, so there is no
  // server session to clean up. It still reserved an intent above: a cancelled
  // newer submission must keep any older login from becoming authoritative.
  if (signal?.aborted) {
    if (isAuthIntentCurrent(authIntentGeneration)) reserveAuthIntent();
    throw signal.reason ?? new DOMException('Login cancelled', 'AbortError');
  }

  const transport = settleLoginAttempt(attempt, body, outgoing);
  return awaitWithCancellation(transport, signal, attempt);
}

interface LoginAttempt {
  authIntentGeneration: number;
  authSlot: string;
  cancelled: boolean;
}

// One browser profile has one selected refresh session. If another tab changes
// that selector, this tab must not keep refreshing/resurrecting its former
// principal: revoke its own family and preserve the newer tab's storage value.
subscribeActiveAuthSlot((selectedSlot) => {
  const localSlot = getAccessTokenAuthSlot();
  const bearer = getAccessToken();
  if (bearer && localSlot && selectedSlot !== localSlot) void logout(bearer);
});

async function settleLoginAttempt(
  attempt: LoginAttempt,
  body: LoginRequest,
  outgoing: { bearer: string | null; authSlot: string | null },
): Promise<TokenResponse> {
  const refreshBarrier = cancelInFlightRefresh();
  if (refreshBarrier) await refreshBarrier;
  const logoutBarrier = awaitLogoutTransition();
  if (logoutBarrier) await logoutBarrier;
  if (attempt.cancelled || !isAuthIntentCurrent(attempt.authIntentGeneration)) {
    throw new Error('Login intent superseded before dispatch');
  }

  let token: TokenResponse;
  try {
    token = await request<TokenResponse>('/auth/login', {
      method: 'POST',
      json: body,
      skipAuth: true,
      headers: authSlotHeaders(attempt.authSlot),
    });
  } catch (error) {
    // A transport failure can occur after Set-Cookie was accepted but before
    // the JSON bearer arrived. Probe only that ambiguous class: if the unique
    // cookie exists, refresh yields a bearer used solely to revoke/delete it.
    // Expected credential/validation/conflict responses never create a cookie
    // and must not cause an extra auth request.
    if (!(error instanceof ApiError) || error.status === 0 || error.status >= 500) {
      void abandonUnknownLoginSlot(attempt.authSlot);
    }
    throw error;
  }
  if (
    attempt.cancelled ||
    !setLoginAccessToken(token.access_token, attempt.authSlot, attempt.authIntentGeneration)
  ) {
    void revokeSession(token.access_token, attempt.authSlot);
    throw new Error('Login result discarded after a newer auth intent');
  }

  // A successful direct account switch retires the previous browser session;
  // a failed attempt above deliberately leaves it intact.
  if (outgoing.bearer && outgoing.bearer !== token.access_token) {
    void revokeSession(outgoing.bearer, outgoing.authSlot);
  }
  return token;
}

function awaitWithCancellation(
  transport: Promise<TokenResponse>,
  signal: AbortSignal | undefined,
  attempt: LoginAttempt,
): Promise<TokenResponse> {
  if (!signal) return transport;
  return new Promise<TokenResponse>((resolve, reject) => {
    const cancel = () => {
      attempt.cancelled = true;
      if (isAuthIntentCurrent(attempt.authIntentGeneration)) reserveAuthIntent();
      reject(signal.reason ?? new DOMException('Login cancelled', 'AbortError'));
    };
    if (signal.aborted) {
      cancel();
      return;
    }
    signal.addEventListener('abort', cancel, { once: true });
    transport.then(resolve, reject).finally(() => signal.removeEventListener('abort', cancel));
  });
}

async function revokeSession(bearer: string, authSlot: string | null): Promise<void> {
  const headers = new Headers(authSlotHeaders(authSlot));
  headers.set('Authorization', `Bearer ${bearer}`);
  await request<void>('/auth/logout', {
    method: 'POST',
    skipAuth: true,
    headers,
  }).catch(() => {
    // The selected cookie remains unselected and expires server-side; best effort.
  });
}

async function abandonUnknownLoginSlot(authSlot: string): Promise<void> {
  try {
    const token = await request<TokenResponse>('/auth/refresh', {
      method: 'POST',
      skipAuth: true,
      headers: authSlotHeaders(authSlot),
    });
    await revokeSession(token.access_token, authSlot);
  } catch {
    // No accepted cookie, or the network is still unavailable. Server TTL is
    // the final bound; no shared/newer cookie is touched.
  }
}

/** Mint a new access token from the refresh cookie (AC-2). Stores the token. */
export async function refresh(_signal?: AbortSignal): Promise<TokenResponse> {
  // Keep the public bootstrap API self-contained. App boot installs this once,
  // while isolated consumers/tests can still call `refresh()` without relying
  // on import order or another component's setup side effect.
  installAuthRefresh();
  return (await runCoordinatedRefresh()) as TokenResponse;
}

async function performRefresh({
  signal,
  principalGeneration,
  authIntentGeneration,
  authSlot,
}: {
  signal: AbortSignal;
  principalGeneration: number;
  authIntentGeneration: number;
  authSlot: string | null;
}): Promise<TokenResponse> {
  const token = await request<TokenResponse>('/auth/refresh', {
    method: 'POST',
    skipAuth: true,
    signal,
    headers: authSlotHeaders(authSlot),
  });
  if (
    !setRefreshedAccessToken(
      token.access_token,
      principalGeneration,
      authIntentGeneration,
      authSlot,
    )
  ) {
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
  const authSlot = getAccessTokenAuthSlot();
  // Publish the complete transition before local teardown or any await. This
  // closes the refresh-cancellation registration gap from R2-002.
  const transition = runLogoutTransition(
    async (signal) => {
      await cancelInFlightRefresh();
      const headers = new Headers(authSlotHeaders(authSlot));
      if (bearer) headers.set('Authorization', `Bearer ${bearer}`);
      await request<void>('/auth/logout', {
        method: 'POST',
        skipAuth: true,
        headers,
        signal,
      }).catch(() => {
        // Best-effort revocation; local logout proceeds regardless.
      });
    },
    { slotScoped: authSlot !== null },
  );
  if (clearLocalToken) clearAccessToken();
  return transition;
}

/**
 * Register the silent-refresh implementation with the client. Idempotent; call
 * once at app boot. Kept here (not in client.ts) so the client stays free of the
 * auth import cycle. A failed refresh clears the token so the guard routes to
 * login.
 */
export function installAuthRefresh(): void {
  registerRefreshHandler(authRefreshHandler);
}

const authRefreshHandler = async (context: Parameters<typeof performRefresh>[0]) => {
  try {
    return await performRefresh(context);
  } catch (error) {
    clearAccessTokenIfPrincipalUnchanged(context.principalGeneration, context.authIntentGeneration);
    throw error;
  }
};
