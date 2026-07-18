/**
 * Server-state hooks for the code-run inspector (#232) — TanStack Query over the
 * typed `@/api` boundary (the ONLY backend caller, ADR-0004). A code run is server
 * data: the full record (code, stdout/stderr, timing, resource usage, artifacts)
 * lives in the query cache, fetched from GET /code-runs/{id}. No transport here.
 *
 * Conforms to the contract (contracts/openapi.yaml §code-runs, ADR-0020).
 * Negative paths surface as typed `ApiError`s the components branch on: a
 * non-owned / cross-tenant / unknown id → 404 (existence non-disclosure,
 * INV-1/INV-2); a malformed id → 422 (INV-8); a missing/expired token → 401
 * (INV-4).
 */
import { useMutation, useQuery, useQueryClient, type UseQueryResult } from '@tanstack/react-query';
import { cancelCodeRun, getCodeRun } from '@/api';
import type { CodeRun } from '@/api';
import { isTerminalCodeRun } from './presentation';

/** Stable query keys for the slice. */
export const codeRunKeys = {
  all: ['code-runs'] as const,
  detail: (id: string) => [...codeRunKeys.all, 'detail', id] as const,
};

/**
 * One code run's full detail. Disabled until an id exists. A non-terminal run
 * (queued/running) polls on an interval so an in-progress run's status + captured
 * output catch up without a manual reload — the light-touch alternative to a
 * WS live-attach for the after-the-fact inspector; the inline chat panel gets its
 * live output from the stream directly (streamReducer).
 */
export function useCodeRun(
  id: string | null,
  options?: { enabled?: boolean; pollMs?: number },
): UseQueryResult<CodeRun> {
  const pollMs = options?.pollMs ?? 3_000;
  return useQuery<CodeRun>({
    queryKey: codeRunKeys.detail(id ?? '∅'),
    queryFn: ({ signal }) => getCodeRun(id as string, signal),
    enabled: id !== null && options?.enabled !== false,
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      if (!status) return false;
      return isTerminalCodeRun(status) ? false : pollMs;
    },
  });
}

/** Explicit cancellation destroys the active container and returns the killed row. */
export function useCancelCodeRun(id: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: () => cancelCodeRun(id),
    onSuccess: (value) => {
      client.setQueryData(codeRunKeys.detail(id), value);
      if (value.session_id) {
        void client.invalidateQueries({ queryKey: ['chat', 'sandbox', value.session_id] });
      }
    },
  });
}
