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
  getAuthIntentGeneration,
  getPrincipalGeneration,
  getRefreshAuthSlot,
  isAuthIntentCurrent,
  reserveAuthIntent,
  setLoginAccessToken,
  setRefreshedAccessToken,
  supersedeForExternalAuthSelection,
} from './token';
import {
  authLoginSlotHeaders,
  authSlotHeaders,
  createAuthSlot,
  getAuthRefreshRevision,
  publishAuthRefresh,
  subscribeActiveAuthSlot,
  waitForAuthRefresh,
  withAuthRefreshLock,
} from './authSlot';
import type { CurrentUser, LoginRequest, TokenResponse } from './types';

/** Exchange email + password for an access token (AC-1). Stores the token. */
export async function login(body: LoginRequest, signal?: AbortSignal): Promise<TokenResponse> {
  // Reserve latest-intent authority and a distinct cookie slot synchronously,
  // before any barrier or transport await. A failed direct login therefore does
  // not surprise-log-out an already authenticated principal, while older login
  // responses can neither commit a token nor overwrite the newer cookie slot.
  const authIntentGeneration = reserveAuthIntent();
  cancelPendingLoginAttempts();
  const authSlot = createAuthSlot();
  const outgoing = {
    bearer: getAccessToken(),
    authSlot: getAccessTokenAuthSlot(),
  };
  const attempt: LoginAttempt = {
    authIntentGeneration,
    authSlot,
    cancelled: false,
    controller: new AbortController(),
  };

  // An already-cancelled caller has not dispatched credentials, so there is no
  // server session to clean up. It still reserved an intent above: a cancelled
  // newer submission must keep any older login from becoming authoritative.
  if (signal?.aborted) {
    if (isAuthIntentCurrent(authIntentGeneration)) reserveAuthIntent();
    throw signal.reason ?? new DOMException('Login cancelled', 'AbortError');
  }

  pendingLoginAttempts.add(attempt);
  const transport = settleLoginAttempt(attempt, body, outgoing).finally(() => {
    pendingLoginAttempts.delete(attempt);
  });
  return awaitWithCancellation(transport, signal, attempt);
}

interface LoginAttempt {
  authIntentGeneration: number;
  authSlot: string;
  cancelled: boolean;
  controller: AbortController;
}

const pendingLoginAttempts = new Set<LoginAttempt>();

function cancelPendingLoginAttempts(): void {
  for (const pending of pendingLoginAttempts) {
    pending.cancelled = true;
    pending.controller.abort(new DOMException('Login superseded', 'AbortError'));
  }
}

// One browser profile has one selected refresh session. If another tab changes
// that selector, this tab must not keep refreshing/resurrecting its former
// principal: revoke its own family and preserve the newer tab's storage value.
subscribeActiveAuthSlot((selectedSlot, previousSlot) => {
  // Everything through here is synchronous: advance both epochs, abort login
  // and refresh transports, and notify PrincipalLifecycle before stale A work
  // can commit after B's storage event. Do this even when both the selected
  // slot and the in-memory token are null: a clear event can race a pre-token
  // bootstrap whose A slot is visible only in ``previousSlot``.
  const outgoing = supersedeForExternalAuthSelection();
  cancelPendingLoginAttempts();
  const refreshBarrier = cancelInFlightRefresh();
  // ``storage.oldValue`` is the browser-profile selection being replaced. The
  // in-memory bearer can briefly lag it (e.g. a prior selector event is still
  // bootstrapping), so never attach that bearer to a different slot id.
  const obsoleteSlot = previousSlot ?? outgoing.authSlot;

  void (async () => {
    if (refreshBarrier) await refreshBarrier;
    if (
      outgoing.bearer &&
      outgoing.authSlot &&
      obsoleteSlot === outgoing.authSlot &&
      obsoleteSlot !== selectedSlot
    ) {
      void revokeSession(outgoing.bearer, obsoleteSlot);
    } else if (obsoleteSlot && obsoleteSlot !== selectedSlot) {
      void abandonUnknownLoginSlot(obsoleteSlot);
    }
    if (selectedSlot) {
      // The selecting tab writes storage only after its HTTP response installed
      // the HttpOnly cookie. Re-bootstrap this document into that principal.
      await refresh().catch(() => undefined);
    }
  })();
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
      headers: authLoginSlotHeaders(attempt.authSlot, outgoing.authSlot),
      signal: attempt.controller.signal,
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
      attempt.controller.abort(signal.reason);
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
  return withAuthRefreshLock(authSlot, async () => {
    let revision = getAuthRefreshRevision(authSlot);
    for (let attempt = 0; attempt < 3; attempt += 1) {
      if (
        signal.aborted ||
        getPrincipalGeneration() !== principalGeneration ||
        getAuthIntentGeneration() !== authIntentGeneration ||
        getRefreshAuthSlot() !== authSlot
      ) {
        throw new ApiError('Principal changed before refresh', 401);
      }

      let token: TokenResponse;
      try {
        token = await request<TokenResponse>('/auth/refresh', {
          method: 'POST',
          skipAuth: true,
          signal,
          headers: authSlotHeaders(authSlot),
        });
      } catch (error) {
        if (
          !(error instanceof ApiError) ||
          error.status !== 401 ||
          error.problem?.code !== 'refresh_superseded' ||
          attempt === 2
        ) {
          throw error;
        }
        // The row is still valid but another document rotated it. Wait for its
        // cookie completion marker; if APIs/storage are unavailable, a bounded
        // delay then retry remains safe and cannot authorize a stolen old token.
        await waitForAuthRefresh(authSlot, revision);
        revision = getAuthRefreshRevision(authSlot);
        continue;
      }

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
      publishAuthRefresh(authSlot);
      return token;
    }
    throw new ApiError('Refresh session remained superseded', 401);
  });
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
