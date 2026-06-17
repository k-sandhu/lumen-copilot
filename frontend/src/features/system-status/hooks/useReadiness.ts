/**
 * Server state for backend readiness — TanStack Query (NOT a store). Refetches
 * periodically so the panel stays live; exposes the full query so the feature
 * can render loading / error / success distinctly (quality bar: every state).
 */
import { useQuery } from '@tanstack/react-query';
import { getReadiness } from '@/api';
import type { ReadinessStatus } from '@/api';

export const readinessQueryKey = ['system-status', 'readiness'] as const;

export function useReadiness() {
  return useQuery<ReadinessStatus>({
    queryKey: readinessQueryKey,
    queryFn: ({ signal }) => getReadiness(signal),
    // Live-ish polling. Retry/refetch policy is inherited from the shared
    // QueryClient (prod retries; tests disable retries so the error state is
    // assertable immediately) — don't override it here.
    refetchInterval: 15_000,
  });
}
