/**
 * Typed per-user account calls — part of the api/ boundary (ADR-0004). The ONLY
 * place the SPA performs `/me/*` HTTP. Conforms to the frozen contract
 * (contracts/openapi.yaml §user):
 *
 *   PUT    /me/avatar  (multipart file) → UserAvatar   (the caller's avatar URL)
 *   DELETE /me/avatar                    → 204          (clear; initials fallback)
 *
 * Always the caller's own account (no id in the path); authenticated but NOT
 * admin-gated. The avatar's current state is also readable via GET /auth/me
 * (`avatar_url`) — the source the shell + settings page read — so the mutations
 * invalidate that query rather than needing a separate GET here.
 */
import { ApiError, request, withRefreshRetry } from './client';
import { API_BASE_URL } from './env';
import { getAccessToken } from './token';
import type { Problem, UserAvatar } from './types';

/**
 * Upload the caller's profile avatar via `PUT /me/avatar` (multipart, authenticated,
 * audited). Uses `fetch` + `FormData` (no JSON body): the browser sets the multipart
 * boundary; the bearer token is attached and the shared 401-refresh-retry policy
 * applies. A 413 (over-size) / 415 (non-image) surfaces as a typed `ApiError`
 * carrying the Problem body. Returns the new `{ avatar_url }`.
 */
export function updateAvatar(file: File): Promise<UserAvatar> {
  return withRefreshRetry<UserAvatar>(async () => {
    const url = joinApi('/me/avatar');
    const headers = new Headers();
    headers.set('Accept', 'application/json, application/problem+json');
    const token = getAccessToken();
    if (token) headers.set('Authorization', `Bearer ${token}`);
    // NB: do NOT set Content-Type — the browser adds the multipart boundary.
    const form = new FormData();
    form.append('file', file);

    let response: Response;
    try {
      response = await fetch(url, {
        method: 'PUT',
        credentials: 'include',
        headers,
        body: form,
      });
    } catch (cause) {
      throw new ApiError(cause instanceof Error ? cause.message : 'Network request failed', 0);
    }
    if (!response.ok) {
      throw new ApiError(
        `Request to ${url} failed with ${response.status}`,
        response.status,
        await safeProblem(response),
      );
    }
    return (await response.json()) as UserAvatar;
  });
}

/**
 * Clear the caller's profile avatar via `DELETE /me/avatar` (authenticated) so the
 * shell reverts to the initials fallback. Idempotent (204).
 */
export function clearAvatar(): Promise<void> {
  return request<void>('/me/avatar', { method: 'DELETE' });
}

function joinApi(path: string): string {
  const b = API_BASE_URL.endsWith('/') ? API_BASE_URL.slice(0, -1) : API_BASE_URL;
  const p = path.startsWith('/') ? path : `/${path}`;
  return `${b}${p}`;
}

function isProblem(value: unknown): value is Problem {
  if (typeof value !== 'object' || value === null) return false;
  const v = value as Record<string, unknown>;
  return typeof v.title === 'string' && typeof v.status === 'number';
}

async function safeProblem(response: Response): Promise<Problem | undefined> {
  try {
    const data: unknown = await response.json();
    if (isProblem(data)) return data;
  } catch {
    // Non-JSON / empty body — fall through.
  }
  return undefined;
}
