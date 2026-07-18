/**
 * Typed sources (connector grid) calls — part of the api/ boundary (ADR-0004).
 * The ONLY place the SPA performs HTTP for the Sources slice. Conforms to the
 * FROZEN contract (contracts/openapi.yaml §sources, ADR-0009 §5 / #108 +
 * ADR-0019 §1 / #451):
 *
 *   GET    /sources            ?cursor&limit  → SourceList
 *   POST   /sources            SourceCreate   → Source (201: web pending | gdrive pending_auth)
 *   POST   /sources/{id}/sync                 → Source (202 enqueued | 200 already syncing)
 *   POST   /sources/{id}/connect              → SourceConnectResponse {authorization_url}
 *   DELETE /sources/{id}                      → 204 (cascades the source's documents)
 *
 * Sources are tenant-scoped (spec 0004 INV-1/INV-2); `web` sources are
 * owner-scoped, while every MANAGED (`gdrive`) mutation — create, connect,
 * sync, delete — is tenant-admin-gated at action time (ADR-0019 §1). Negative
 * paths surface as typed `ApiError`s the UI branches on (problem body, not
 * status text): a missing/expired token → 401 (INV-4); an invalid OR
 * SSRF-blocked URL / invalid gdrive config on add → 422 (INV-8); a non-admin on
 * a managed mutation → 403 (INV-5); a non-owner / cross-tenant source → 404
 * (INV-1/INV-2); connect on a `web` source → 409 `oauth_not_supported` (INV-8).
 */
import { request } from './client';
import type { Source, SourceConnectResponse, SourceCreate, SourceList } from './types';

/** Cursor-page params for the list endpoint. */
export interface PageQuery {
  cursor?: string;
  limit?: number;
}

function buildQuery(params: Record<string, string | number | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== '') search.set(key, String(value));
  }
  const qs = search.toString();
  return qs ? `?${qs}` : '';
}

/** List the caller's connected sources (one cursor page). */
export function listSources(page: PageQuery = {}, signal?: AbortSignal): Promise<SourceList> {
  return request<SourceList>(`/sources${buildQuery({ ...page })}`, { signal });
}

/**
 * Add a source. `web`: `{ type: 'web', url }` — the server validates +
 * SSRF-checks the URL (an invalid / blocked URL → 422) and enqueues the first
 * sync; the returned Source is `pending`. `gdrive` (tenant admin only — a
 * non-admin → 403, INV-5): `{ type: 'gdrive', config }` — the returned Source
 * is `pending_auth`; run `connectSource` next to obtain the consent URL.
 */
export function createSource(body: SourceCreate): Promise<Source> {
  return request<Source>('/sources', { method: 'POST', json: body });
}

/**
 * Start (or restart) a managed source's OAuth consent flow (ADR-0019 §1; admin
 * only, 403 otherwise). Returns `{authorization_url}` the BROWSER must navigate
 * to; the provider redirects back to the SPA sources route with the frozen
 * `connect=ok|error` query params. Also the repair path for a source flagged
 * `reauthorize_required`. A `web` source → 409 `oauth_not_supported` (INV-8).
 */
export function connectSource(id: string): Promise<SourceConnectResponse> {
  return request<SourceConnectResponse>(`/sources/${id}/connect`, { method: 'POST' });
}

/**
 * Re-sync a source (re-fetch + re-index). Returns the source with status
 * `syncing` (202 when a re-sync was enqueued, 200 when it was already syncing).
 */
export function syncSource(id: string): Promise<Source> {
  return request<Source>(`/sources/${id}/sync`, { method: 'POST' });
}

/** Remove a source and cascade its ingested documents (204). */
export function deleteSource(id: string): Promise<void> {
  return request<void>(`/sources/${id}`, { method: 'DELETE' });
}
