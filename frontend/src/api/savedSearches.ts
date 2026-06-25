/**
 * Typed saved-searches calls — part of the api/ boundary (ADR-0004). The ONLY
 * place the SPA performs saved-search HTTP. Conforms to the frozen contract
 * (contracts/openapi.yaml §saved-searches, spec 0005):
 *
 *   GET    /saved-searches            → SavedSearchList (cursor-paginated)
 *   POST   /saved-searches            → SavedSearch     (201)
 *   GET    /saved-searches/{id}       → SavedSearch
 *   PATCH  /saved-searches/{id}       → SavedSearch
 *   DELETE /saved-searches/{id}       → 204
 *
 * Per-user + tenant-scoped: a cross-tenant / other-user id returns 404
 * (INV-1/INV-2), surfaced as an ApiError.
 */
import { request } from './client';
import type { PageParams } from './chat';
import type {
  SavedSearch,
  SavedSearchCreate,
  SavedSearchList,
  SavedSearchUpdate,
} from './types';

function pageQuery(params?: PageParams): string {
  if (!params) return '';
  const q = new URLSearchParams();
  if (params.cursor) q.set('cursor', params.cursor);
  if (params.limit !== undefined) q.set('limit', String(params.limit));
  const s = q.toString();
  return s ? `?${s}` : '';
}

/** List the caller's saved searches (newest-updated first). */
export function listSavedSearches(
  params?: PageParams,
  signal?: AbortSignal,
): Promise<SavedSearchList> {
  return request<SavedSearchList>(`/saved-searches${pageQuery(params)}`, { signal });
}

/** Save a search (query + optional filters) under a name. */
export function createSavedSearch(body: SavedSearchCreate): Promise<SavedSearch> {
  return request<SavedSearch>('/saved-searches', { method: 'POST', json: body });
}

/** Get one saved search (404 if not yours / cross-tenant — INV-1/INV-2). */
export function getSavedSearch(id: string, signal?: AbortSignal): Promise<SavedSearch> {
  return request<SavedSearch>(`/saved-searches/${id}`, { signal });
}

/** Rename a saved search or change its query/filters. */
export function updateSavedSearch(id: string, body: SavedSearchUpdate): Promise<SavedSearch> {
  return request<SavedSearch>(`/saved-searches/${id}`, { method: 'PATCH', json: body });
}

/** Delete a saved search. */
export function deleteSavedSearch(id: string): Promise<void> {
  return request<void>(`/saved-searches/${id}`, { method: 'DELETE' });
}
