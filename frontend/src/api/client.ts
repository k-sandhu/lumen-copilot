/**
 * Small typed fetch wrapper — part of the api/ boundary (ADR-0004): the ONLY
 * place the SPA performs HTTP to the backend. Features call typed functions in
 * sibling modules (e.g. `health.ts`); they never touch `fetch` directly.
 *
 * Errors are parsed into a typed `ApiError` carrying the RFC-9457 `Problem`
 * body when the server sends one, so callers branch on shape, not status codes.
 */
import { API_BASE_URL } from './env';
import type { Problem } from './types';

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
  /** JSON body — serialized and sent with application/json. */
  json?: unknown;
  /** Treat these non-2xx statuses as success (e.g. 503 readiness still parses). */
  okStatuses?: number[];
}

/**
 * Perform a JSON request against the backend and parse the response.
 *
 * @param path  Path relative to VITE_API_BASE_URL OR an absolute "/health..."
 *              path (used for liveness/readiness which live outside /api).
 */
export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { json, okStatuses = [], headers, ...init } = options;

  // Absolute same-origin paths (e.g. "/health/ready") bypass the API base.
  const url = path.startsWith('/') ? path : joinUrl(API_BASE_URL, path);

  const finalHeaders = new Headers(headers);
  finalHeaders.set('Accept', 'application/json, application/problem+json');

  let body: BodyInit | undefined;
  if (json !== undefined) {
    finalHeaders.set('Content-Type', 'application/json');
    body = JSON.stringify(json);
  }

  let response: Response;
  try {
    response = await fetch(url, { ...init, headers: finalHeaders, body });
  } catch (cause) {
    // Network/CORS/abort — no HTTP status to speak of.
    throw new ApiError(
      cause instanceof Error ? cause.message : 'Network request failed',
      0,
    );
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
}

async function safeParseProblem(response: Response): Promise<Problem | undefined> {
  try {
    const data: unknown = await response.json();
    if (isProblem(data)) return data;
  } catch {
    // Non-JSON or empty body — fall through to undefined.
  }
  return undefined;
}
