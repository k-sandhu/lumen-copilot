import { QueryClient } from '@tanstack/react-query';

/** Exponential, capped retry backoff — same shape as the WS reconnect delay
 * (`api/ws.ts`: `Math.min(maxMs, baseMs * 2 ** attempt)`). With `retry: 1` only
 * `retryDelay(0)` fires today, but the curve is defined so raising `retry` later
 * keeps the backoff. `attempt` is 0-based. */
export function retryDelay(attempt: number): number {
  return Math.min(30_000, 1_000 * 2 ** attempt);
}

/** Shared TanStack Query client. Sensible defaults for a read-mostly skeleton. */
export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 5_000,
      retry: 1,
      // Back off before the retry instead of hammering immediately on a transient
      // failure (the default retryDelay is already exponential, but pin it
      // explicitly so the curve + ceiling match the WS client and stay stable).
      retryDelay,
      refetchOnWindowFocus: false,
    },
  },
});
