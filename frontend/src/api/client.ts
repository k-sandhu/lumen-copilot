/**
 * Small typed fetch wrapper - part of the api/ boundary (ADR-0004): the ONLY
 * place the SPA performs HTTP to the backend. Features call typed functions in
 * sibling modules (e.g. `health.ts`, `auth.ts`); they never touch `fetch`.
 *
 * Errors are parsed into a typed `ApiError` carrying the RFC-9457 `Problem`
 * body when the server sends one, so callers branch on shape, not status codes.
 *
 * AUTH WIRING (issue #48, spec 0004 section 2.3):
 *   - Every request carries `credentials: 'include'` so the httpOnly refresh
 *     cookie rides along (login sets it; refresh consumes it).
 *   - When an access token is held it is sent as `Authorization: Bearer ...`
 *     (unless `skipAuth` - login/refresh are unauthenticated).
 *   - On a 401 (INV-4: missing/expired token), the client performs ONE silent
 *     refresh via the registered handler, then retries the original request
 *     once. A failed refresh propagates the 401 so the caller routes to login.
 */
import { API_BASE_URL } from './env';
import {
  getAccessToken,
  getAuthIntentGeneration,
  getPrincipalGeneration,
  getRefreshAuthSlot,
} from './token';
import type { Problem } from './types';

/**
 * Refresh hook registered by `auth.ts` to break the client/auth import cycle.
 * It must mint+conditionally store a new access token (or throw). `null`
 * disables refresh. The coordinator owns the AbortController and identity epoch.
 */
interface RefreshContext {
  signal: AbortSignal;
  principalGeneration: number;
  authIntentGeneration: number;
  authSlot: string | null;
}

type RefreshHandler = ((context: RefreshContext) => Promise<unknown>) | null;
let refreshHandler: RefreshHandler = null;

interface InFlightRefresh {
  controller: AbortController;
  principalGeneration: number;
  authIntentGeneration: number;
  authSlot: string | null;
  promise: Promise<unknown>;
}

let inFlightRefresh: InFlightRefresh | null = null;

interface InFlightLogout {
  controller: AbortController;
  slotScoped: boolean;
  promise: Promise<void>;
}

let inFlightLogout: InFlightLogout | null = null;
const AUTH_TRANSITION_WAIT_MS = 1_500;

async function settlesWithin(promise: Promise<unknown>): Promise<boolean> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  const timeout = new Promise<false>((resolve) => {
    timer = setTimeout(() => resolve(false), AUTH_TRANSITION_WAIT_MS);
  });
  const settled = promise.then(
    () => true as const,
    () => true as const,
  );
  const result = await Promise.race([settled, timeout]);
  if (timer !== undefined) clearTimeout(timer);
  return result;
}

/** Wire the silent-refresh implementation (called once from auth.ts setup). */
export function registerRefreshHandler(handler: RefreshHandler): void {
  if (refreshHandler === handler) return;
  inFlightRefresh?.controller.abort();
  inFlightRefresh = null;
  refreshHandler = handler;
}

/** A single in-flight refresh shared across concurrent 401s (no thundering herd). */
async function runRefresh(
  principalGeneration: number,
  authIntentGeneration: number,
  authSlot: string | null,
): Promise<unknown> {
  if (!refreshHandler) throw new ApiError('No refresh handler registered', 401);
  if (
    getPrincipalGeneration() !== principalGeneration ||
    getAuthIntentGeneration() !== authIntentGeneration ||
    getRefreshAuthSlot() !== authSlot
  ) {
    throw new ApiError('Principal changed before refresh', 401);
  }

  const existing = inFlightRefresh;
  if (existing) {
    if (
      existing.principalGeneration === principalGeneration &&
      existing.authIntentGeneration === authIntentGeneration &&
      existing.authSlot === authSlot
    ) {
      return existing.promise;
    }
    existing.controller.abort();
    await settlesWithin(existing.promise);
    if (
      getPrincipalGeneration() !== principalGeneration ||
      getAuthIntentGeneration() !== authIntentGeneration ||
      getRefreshAuthSlot() !== authSlot
    ) {
      throw new ApiError('Principal changed before refresh', 401);
    }
  }

  const controller = new AbortController();
  const handler = refreshHandler;
  const promise = handler({
    signal: controller.signal,
    principalGeneration,
    authIntentGeneration,
    authSlot,
  })
    .then((result) => {
      if (
        getPrincipalGeneration() !== principalGeneration ||
        getAuthIntentGeneration() !== authIntentGeneration ||
        getRefreshAuthSlot() !== authSlot
      ) {
        throw new ApiError('Principal changed during refresh', 401);
      }
      return result;
    })
    .finally(() => {
      if (inFlightRefresh?.promise === promise) inFlightRefresh = null;
    });
  inFlightRefresh = {
    controller,
    principalGeneration,
    authIntentGeneration,
    authSlot,
    promise,
  };
  return promise;
}

/** The only public entry to a cookie-rotating refresh, including bootstrap. */
export function runCoordinatedRefresh(): Promise<unknown> {
  return runRefresh(getPrincipalGeneration(), getAuthIntentGeneration(), getRefreshAuthSlot());
}

/**
 * Abort the browser fetch for the old principal and wait for its JS promise to
 * settle before a login/logout request is issued. This is transport cleanup,
 * not a claim that the server rolled back work or an already-accepted cookie.
 */
export function cancelInFlightRefresh(): Promise<void> | null {
  const existing = inFlightRefresh;
  if (!existing) return null;
  return (async () => {
    existing.controller.abort();
    const settled = await settlesWithin(existing.promise);
    if (!settled && inFlightRefresh === existing) {
      // Detach the uncooperative old promise after bounding the wait. Its token
      // commit/retry remains generation-gated even if it resolves much later.
      inFlightRefresh = null;
    }
  })();
}

/** Run the cookie-clearing logout request as a coordinated transition. */
export function runLogoutTransition(
  run: (signal: AbortSignal) => Promise<void>,
  options: { slotScoped?: boolean } = {},
): Promise<void> {
  const controller = new AbortController();
  // Defer execution by one microtask so the transition record is observable
  // synchronously at logout intent, before `run` reaches its first await.
  const promise = Promise.resolve()
    .then(() => run(controller.signal))
    .finally(() => {
      if (inFlightLogout?.promise === promise) inFlightLogout = null;
    });
  inFlightLogout = { controller, slotScoped: options.slotScoped === true, promise };
  return promise;
}

/**
 * Ensure an old logout response (which may expire the refresh cookie) lands
 * before a later login response sets the next principal's cookie.
 */
export function awaitLogoutTransition(): Promise<void> | null {
  const existing = inFlightLogout;
  if (!existing) return null;
  // A slot-scoped logout can only delete/revoke its own unique cookie/row. The
  // server protocol, not response timing, makes it safe to dispatch a new login.
  if (existing.slotScoped) return null;
  return (async () => {
    const settled = await settlesWithin(existing.promise);
    if (settled) return;

    // A hung response cannot wedge the next login forever. Slot-scoped server
    // cookies make a late response incapable of mutating the incoming slot.
    existing.controller.abort();
    if (inFlightLogout === existing) inFlightLogout = null;
  })();
}

/** Typed transport error. `problem` is present when the server returned one. */
export class ApiError extends Error {
  readonly status: number;
  readonly problem?: Problem;

  constructor(message: string, status: number, problem?: Problem) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.problem = problem;
  }

  /** Best human-readable message: problem detail/title, else the raw message. */
  get displayMessage(): string {
    return this.problem?.detail ?? this.problem?.title ?? this.message;
  }
}

function isProblem(value: unknown): value is Problem {
  if (typeof value !== 'object' || value === null) return false;
  const v = value as Record<string, unknown>;
  return typeof v.title === 'string' && typeof v.status === 'number';
}

function joinUrl(base: string, path: string): string {
  const b = base.endsWith('/') ? base.slice(0, -1) : base;
  const p = path.startsWith('/') ? path : `/${path}`;
  return `${b}${p}`;
}

export interface RequestOptions extends Omit<RequestInit, 'body'> {
  /** JSON body - serialized and sent with application/json. */
  json?: unknown;
  /** Treat these non-2xx statuses as success (e.g. 503 readiness still parses). */
  okStatuses?: number[];
  /**
   * Skip the bearer header AND the 401-refresh-retry. Used by the auth
   * endpoints themselves (login/refresh) so refresh never recurses.
   */
  skipAuth?: boolean;
}

/**
 * Run one authenticated operation with the shared 401 refresh policy.
 *
 * The operation signals an expired token by throwing `ApiError` status 401.
 * This helper refreshes once, reruns the operation once with `isRetry=true`,
 * and otherwise preserves the original error.
 */
export async function withRefreshRetry<T>(
  run: (isRetry: boolean) => Promise<T>,
  opts: { skipAuth?: boolean } = {},
): Promise<T> {
  const principalGeneration = getPrincipalGeneration();
  const authIntentGeneration = getAuthIntentGeneration();
  const authSlot = getRefreshAuthSlot();
  try {
    const result = await run(false);
    if (!opts.skipAuth && getPrincipalGeneration() !== principalGeneration) {
      throw new ApiError('Principal changed while request was in flight', 401);
    }
    return result;
  } catch (error) {
    if (
      !(error instanceof ApiError) ||
      error.status !== 401 ||
      opts.skipAuth === true ||
      !refreshHandler
    ) {
      throw error;
    }

    if (getPrincipalGeneration() !== principalGeneration) throw error;

    try {
      await runRefresh(principalGeneration, authIntentGeneration, authSlot);
    } catch {
      throw error;
    }

    if (getPrincipalGeneration() !== principalGeneration) throw error;
    const result = await run(true);
    if (getPrincipalGeneration() !== principalGeneration) throw error;
    return result;
  }
}

/**
 * Perform a JSON request against the backend and parse the response.
 *
 * @param path  Path relative to VITE_API_BASE_URL OR an absolute "/health..."
 *              path (used for liveness/readiness which live outside /api).
 */
export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { json, okStatuses = [], headers, skipAuth = false, ...init } = options;

  // Health/readiness live outside the versioned API base (they're proxied at
  // "/health"); every other path is relative to API_BASE_URL (the "/api/v1"
  // mount), whether or not it has a leading slash. Without this, leading-slash
  // feature paths like "/auth/login" would hit the SPA origin and 404.
  const url = path.startsWith('/health') ? path : joinUrl(API_BASE_URL, path);

  return withRefreshRetry<T>(
    async () => {
      const finalHeaders = new Headers(headers);
      finalHeaders.set('Accept', 'application/json, application/problem+json');

      // Bearer the access token onto every authenticated request (AC-1).
      if (!skipAuth) {
        const token = getAccessToken();
        if (token) finalHeaders.set('Authorization', `Bearer ${token}`);
      }

      let body: BodyInit | undefined;
      if (json !== undefined) {
        finalHeaders.set('Content-Type', 'application/json');
        body = JSON.stringify(json);
      }

      let response: Response;
      try {
        // credentials:'include' carries the httpOnly refresh cookie (spec 0004).
        response = await fetch(url, {
          credentials: 'include',
          ...init,
          headers: finalHeaders,
          body,
        });
      } catch (cause) {
        // Network/CORS/abort: no HTTP status to speak of.
        throw new ApiError(cause instanceof Error ? cause.message : 'Network request failed', 0);
      }

      const accepted = response.ok || okStatuses.includes(response.status);

      if (!accepted) {
        const problem = await safeParseProblem(response);
        throw new ApiError(
          `Request to ${url} failed with ${response.status}`,
          response.status,
          problem,
        );
      }

      if (response.status === 204) {
        return undefined as T;
      }

      return (await response.json()) as T;
    },
    { skipAuth },
  );
}

async function safeParseProblem(response: Response): Promise<Problem | undefined> {
  try {
    const data: unknown = await response.json();
    if (isProblem(data)) return data;
  } catch {
    // Non-JSON or empty body - fall through to undefined.
  }
  return undefined;
}
