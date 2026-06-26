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
import {
  keepPreviousData,
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from '@tanstack/react-query';
import {
  clearRecentSearches,
  createSavedSearch,
  deleteSavedSearch,
  hasAccessToken,
  listCollections,
  listRecentSearches,
  listSavedSearches,
  search,
  suggestSearch,
} from '@/api';
import type {
  CollectionList,
  RecentSearchList,
  SavedSearch,
  SavedSearchCreate,
  SavedSearchList,
  SearchQuery,
  SearchResponse,
  SuggestResponse,
} from '@/api';

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

/** Query key for the collection-scope filter list (shared with the documents slice). */
export const searchCollectionsKey = ['collections'] as const;

/**
 * The caller's collections, for the search scope filter. Backs the ONLY
 * collection-level filter the contract supports (`GET /search?collection_id`);
 * the list itself comes from `GET /collections` via the api/ boundary. Read-only,
 * cached server state — never mirrored into a store.
 */
export function useSearchCollections(): UseQueryResult<CollectionList> {
  return useQuery<CollectionList>({
    queryKey: searchCollectionsKey,
    queryFn: ({ signal }) => listCollections({}, signal),
    staleTime: 30_000,
  });
}

// --- Typeahead suggestions, recent history, saved searches (spec 0005) ------

/**
 * Server-side typeahead suggestions for the (debounced) partial query. Disabled
 * for a blank query so we never fire the 422-on-empty path; the AbortSignal
 * cancels a superseded keystroke's request. `q` should already be debounced by
 * the caller (the combobox). Suggestions are permission-trimmed by the backend.
 */
export function useSuggest(q: string): UseQueryResult<SuggestResponse> {
  const trimmed = q.trim();
  return useQuery<SuggestResponse>({
    queryKey: ['suggest', trimmed] as const,
    queryFn: ({ signal }) => suggestSearch(trimmed, 8, signal),
    enabled: hasAccessToken() && trimmed.length > 0,
    staleTime: 10_000,
  });
}

export const recentSearchesKey = ['recent-searches'] as const;

/** The caller's recent search queries (typeahead "Recent" group). */
export function useRecentSearches(): UseQueryResult<RecentSearchList> {
  return useQuery<RecentSearchList>({
    queryKey: recentSearchesKey,
    queryFn: ({ signal }) => listRecentSearches(10, signal),
    enabled: hasAccessToken(),
    staleTime: 10_000,
  });
}

/** Clear the caller's recent history; refreshes the recent list. */
export function useClearRecentSearches(): UseMutationResult<void, unknown, void> {
  const queryClient = useQueryClient();
  return useMutation<void, unknown, void>({
    mutationFn: () => clearRecentSearches(),
    onSuccess: () => {
      queryClient.setQueryData<RecentSearchList>(recentSearchesKey, { items: [] });
    },
  });
}

export const savedSearchesKey = ['saved-searches'] as const;

/** The caller's saved searches. */
export function useSavedSearches(): UseQueryResult<SavedSearchList> {
  return useQuery<SavedSearchList>({
    queryKey: savedSearchesKey,
    queryFn: ({ signal }) => listSavedSearches(undefined, signal),
    enabled: hasAccessToken(),
    staleTime: 30_000,
  });
}

/** Save the current search (name + query + filters); refreshes the saved list. */
export function useCreateSavedSearch(): UseMutationResult<SavedSearch, unknown, SavedSearchCreate> {
  const queryClient = useQueryClient();
  return useMutation<SavedSearch, unknown, SavedSearchCreate>({
    mutationFn: (body) => createSavedSearch(body),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: savedSearchesKey }),
  });
}

/** Delete a saved search; refreshes the saved list. */
export function useDeleteSavedSearch(): UseMutationResult<void, unknown, string> {
  const queryClient = useQueryClient();
  return useMutation<void, unknown, string>({
    mutationFn: (id) => deleteSavedSearch(id),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: savedSearchesKey }),
  });
}
