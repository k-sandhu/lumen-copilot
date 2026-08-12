/**
 * Server-state hooks for the artifacts slice (#222) — TanStack Query over the
 * typed `@/api` boundary (the ONLY backend caller, ADR-0004). Artifacts are server
 * data: the list lives in the query cache, fetched from GET /artifacts; the delete
 * mutation invalidates the list so it stays fresh. No transport here.
 *
 * Conforms to the FROZEN contract (contracts/openapi.yaml §artifacts, CC-B #208).
 * Negative paths surface as typed `ApiError`s the components branch on: a
 * non-owned / cross-tenant / unknown id → 404 (existence non-disclosure,
 * INV-1/INV-2); a malformed id → 422 (INV-8); a missing/expired token → 401 (INV-4).
 */
import {
  keepPreviousData,
  useQuery,
  useQueryClient,
  type UseQueryResult,
} from '@tanstack/react-query';
import { deleteArtifact, listArtifacts } from '@/api';
import type { ArtifactList, ArtifactListQuery, ArtifactProducedBy } from '@/api';
import { usePrincipalMutation } from '@/lib/usePrincipalMutation';

/** Filters the panel exposes (session scope + write origin). */
export interface ArtifactFilters {
  /** Restrict to artifacts produced in one chat session. */
  sessionId?: string;
  /** Restrict to one write origin (chat_session / run / tool). */
  producedBy?: ArtifactProducedBy;
}

/** Stable query keys for the slice. */
export const artifactKeys = {
  all: ['artifacts'] as const,
  list: (filters: ArtifactFilters) =>
    [...artifactKeys.all, 'list', filters.sessionId ?? null, filters.producedBy ?? null] as const,
};

/** The caller's produced artifacts (one cursor page), optionally filtered (AC-1). */
export function useArtifacts(filters: ArtifactFilters = {}): UseQueryResult<ArtifactList> {
  const query: ArtifactListQuery = {
    session_id: filters.sessionId,
    produced_by: filters.producedBy,
  };
  return useQuery<ArtifactList>({
    queryKey: artifactKeys.list(filters),
    queryFn: ({ signal }) => listArtifacts(query, signal),
    staleTime: 5_000,
    // Keep the previous rows on screen while a refined filter loads, so toggling
    // the origin filter never flashes the list to skeletons.
    placeholderData: keepPreviousData,
  });
}

/** Delete an artifact (stored object + row); invalidates every artifact list (AC-3). */
export function useDeleteArtifact() {
  const qc = useQueryClient();
  return usePrincipalMutation<void, unknown, string>({
    mutationFn: (id) => deleteArtifact(id),
    onSuccess: () => void qc.invalidateQueries({ queryKey: artifactKeys.all }),
  });
}
