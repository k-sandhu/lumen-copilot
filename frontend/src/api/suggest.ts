/**
 * Typed search-typeahead + recent-history calls — part of the api/ boundary
 * (ADR-0004). Conforms to the frozen contract (contracts/openapi.yaml §search,
 * spec 0005):
 *
 *   GET    /search/suggest?q&limit → SuggestResponse  (permission-trimmed typeahead)
 *   GET    /search/recent?limit    → RecentSearchList  (the caller's recents)
 *   DELETE /search/recent          → 204               (clear history)
 *
 * `suggest` document matches are permission-trimmed through the retrieval
 * chokepoint (INV-2); a blank `q` is a 422. Recents are per-user (INV-2).
 */
import { request } from './client';
import type { RecentSearchList, SuggestResponse } from './types';

/**
 * Typeahead suggestions for a partial query. The caller should debounce and pass
 * the AbortSignal of a per-keystroke controller so superseded calls are cancelled.
 */
export function suggestSearch(
  q: string,
  limit?: number,
  signal?: AbortSignal,
): Promise<SuggestResponse> {
  const params = new URLSearchParams({ q });
  if (limit !== undefined) params.set('limit', String(limit));
  return request<SuggestResponse>(`/search/suggest?${params.toString()}`, { signal });
}

/** The caller's recent search queries, newest first. */
export function listRecentSearches(limit?: number, signal?: AbortSignal): Promise<RecentSearchList> {
  const qs = limit !== undefined ? `?limit=${limit}` : '';
  return request<RecentSearchList>(`/search/recent${qs}`, { signal });
}

/** Clear the caller's recent search history (idempotent). */
export function clearRecentSearches(): Promise<void> {
  return request<void>('/search/recent', { method: 'DELETE' });
}
