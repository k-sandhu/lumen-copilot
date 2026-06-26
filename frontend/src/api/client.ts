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
import { getAccessToken } from './token';
import type { Problem } from './types';

/**
 * Refresh hook registered by `auth.ts` to break the client/auth import cycle.
 * It must mint+store a new access token (or throw). `null` disables refresh.
 */
type RefreshHandler = (() => Promise<void>) | null;
let refreshHandler: RefreshHandler = null;

/** Wire the silent-refresh implementation (called once from auth.ts setup). */
export function registerRefreshHandler(handler: RefreshHandler): void {
  refreshHandler = handler;
}

/** A single in-flight refresh shared across concurrent 401s (no thundering herd). */
let inFlightRefresh: Promise<void> | null = null;

async function runRefresh(): Promise<void> {
  if (!refreshHandler) throw new ApiError('No refresh handler registered', 401);
  if (!inFlightRefresh) {
    inFlightRefresh = refreshHandler().finally(() => {
      inFlightRefresh = null;
    });
  }
  return inFlightRefresh;
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
  try {
    return await run(false);
  } catch (error) {
    if (
      !(error instanceof ApiError) ||
      error.status !== 401 ||
      opts.skipAuth === true ||
      !refreshHandler
    ) {
      throw error;
    }

    try {
      await runRefresh();
    } catch {
      throw error;
    }

    return run(true);
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
