import { QueryClient } from '@tanstack/react-query';

/** Shared TanStack Query client. Sensible defaults for a read-mostly skeleton. */
export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 5_000,
      retry: 1,
      refetchOnWindowFocus: false,
    },
  },
});
