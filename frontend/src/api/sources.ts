/**
 * Typed sources (connector grid) calls — part of the api/ boundary (ADR-0004).
 * The ONLY place the SPA performs HTTP for the Sources slice. Conforms to the
 * FROZEN contract (contracts/openapi.yaml §sources, ADR-0009 §5 / #108):
 *
 *   GET    /sources            ?cursor&limit → SourceList
 *   POST   /sources            {type, url}   → Source (201, status pending)
 *   POST   /sources/{id}/sync                → Source (202 enqueued | 200 already syncing)
 *   DELETE /sources/{id}                     → 204 (cascades the source's documents)
 *
 * Sources are tenant- and owner-scoped (spec 0004 INV-1/INV-2): a caller only
 * sees / mutates their own sources within their tenant. Negative paths surface as
 * typed `ApiError`s the UI branches on (problem body, not status text): a
 * missing/expired token → 401 (INV-4); an invalid OR SSRF-blocked URL on add →
 * 422 (INV-8, ADR-0009 §3 — a blocked URL is a validation failure, not a 403);
 * a non-owner / cross-tenant source on sync/delete → 404 (INV-1/INV-2).
 */
import { request } from './client';
import type { Source, SourceCreate, SourceList } from './types';

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
 * Connect a source. For the `web` connector: `{ type: 'web', url }`. The server
 * validates + SSRF-checks the URL (an invalid / blocked URL → 422) and enqueues
 * the first sync; the returned Source has status `pending`.
 */
export function createSource(body: SourceCreate): Promise<Source> {
  return request<Source>('/sources', { method: 'POST', json: body });
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
