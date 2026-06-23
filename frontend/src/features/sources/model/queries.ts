/**
 * Server-state hooks for the Sources slice (#27, ADR-0009) — TanStack Query over
 * the typed `@/api` boundary (the ONLY backend caller, ADR-0004). Sources are
 * server data: the list lives in the query cache, and the create/sync/delete
 * mutations invalidate it so the connector grid stays current. No transport here.
 *
 * Conforms to the FROZEN contract (contracts/openapi.yaml §sources): GET /sources
 * (cursor page), POST /sources (201 pending), POST /sources/{id}/sync (200/202
 * syncing), DELETE /sources/{id} (204). Negative paths surface as typed
 * `ApiError`s the components branch on (a 422 on add → inline form error,
 * ADR-0009 §3; a 404 on sync/delete → existence non-disclosure, INV-1/INV-2).
 *
 * SYNC POLLING: while any source is still `pending` or `syncing`, the list
 * refetches on an interval so the pending → syncing → ready/error transitions
 * surface; it goes quiet once every source is settled.
 */
import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseQueryResult,
} from '@tanstack/react-query';
import { createSource, deleteSource, listSources, syncSource } from '@/api';
import type { Source, SourceCreate, SourceList } from '@/api';

/** Stable query key for the sources list. */
export const sourcesKey = ['sources'] as const;

/** How often to re-poll the list while a sync is in flight (ms). */
export const SYNC_POLL_INTERVAL_MS = 4_000;

function hasUnsettled(list: SourceList | undefined): boolean {
  return (list?.items ?? []).some((s) => s.status === 'pending' || s.status === 'syncing');
}

/**
 * The caller's connected sources (the connector grid, AC-1). Polls while anything
 * is still syncing and stops once settled. The api/ boundary surfaces 401 as a
 * typed `ApiError` the screen renders as an actionable error state.
 */
export function useSources(): UseQueryResult<SourceList> {
  return useQuery<SourceList>({
    queryKey: sourcesKey,
    queryFn: ({ signal }) => listSources({}, signal),
    staleTime: 2_000,
    refetchInterval: (q) => (hasUnsettled(q.state.data) ? SYNC_POLL_INTERVAL_MS : false),
  });
}

/**
 * Connect a source (AC-1). On success the new source comes back `pending`; we
 * invalidate the list so it appears immediately and the poll picks up its sync.
 * A 422 (invalid / SSRF-blocked URL, ADR-0009 §3) propagates as an `ApiError` the
 * Add-source form renders inline — it is NOT swallowed here.
 */
export function useCreateSource() {
  const qc = useQueryClient();
  return useMutation<Source, unknown, SourceCreate>({
    mutationFn: (body) => createSource(body),
    onSuccess: () => void qc.invalidateQueries({ queryKey: sourcesKey }),
  });
}

/** Re-sync a source (AC-1). Refreshes the list so its status flips to syncing. */
export function useSyncSource() {
  const qc = useQueryClient();
  return useMutation<Source, unknown, string>({
    mutationFn: (id) => syncSource(id),
    onSuccess: () => void qc.invalidateQueries({ queryKey: sourcesKey }),
  });
}

/** Remove a source and cascade its documents (AC-1). Refreshes the grid. */
export function useDeleteSource() {
  const qc = useQueryClient();
  return useMutation<void, unknown, string>({
    mutationFn: (id) => deleteSource(id),
    onSuccess: () => void qc.invalidateQueries({ queryKey: sourcesKey }),
  });
}
