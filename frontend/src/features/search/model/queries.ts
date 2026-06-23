/**
 * Server-state hook for the search slice (#84) — TanStack Query over the `search`
 * api/ boundary (the ONLY backend caller; frontend/AGENTS.md). Search results are
 * server data, so they live in the query cache — never mirrored into a store. The
 * only client-side state (the draft query in the composer) lives in the component.
 *
 * The query is DISABLED until a non-empty `q` is submitted, so we never fire the
 * 422-on-empty-q path the contract defines (spec 0004 INV-8) just from mounting.
 * `q` is trimmed and carried in the query key, so each distinct submitted query
 * is cached independently and an in-flight request is cancelled via the signal.
 */
import { keepPreviousData, useQuery, type UseQueryResult } from '@tanstack/react-query';
import { search } from '@/api';
import type { SearchQuery, SearchResponse } from '@/api';

/** Stable query key for one submitted search (filters included for cache split). */
export function searchKey(query: SearchQuery) {
  return [
    'search',
    query.q.trim(),
    query.collection_id ?? null,
    query.source ?? null,
    query.type ?? null,
    query.cursor ?? null,
    query.limit ?? null,
  ] as const;
}

/**
 * Run a permission-trimmed ranked search. Enabled only once `q` is non-empty —
 * an empty/whitespace query keeps the screen in its initial empty state rather
 * than issuing a request that the contract answers with 422.
 */
export function useSearch(query: SearchQuery): UseQueryResult<SearchResponse> {
  const trimmed = query.q.trim();
  return useQuery<SearchResponse>({
    queryKey: searchKey(query),
    queryFn: ({ signal }) => search({ ...query, q: trimmed }, signal),
    enabled: trimmed.length > 0,
    // Keep the prior page visible while a refined query loads (no flash to empty).
    placeholderData: keepPreviousData,
    staleTime: 30_000,
  });
}
