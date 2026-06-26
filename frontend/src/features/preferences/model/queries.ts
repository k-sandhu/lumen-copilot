/**
 * Server-state hooks for account preferences (spec 0005, epic #144) — TanStack
 * Query, the caller's own row. A small cross-feature slice (the chat model picker
 * reads the default model; a future settings surface edits it), mirroring how the
 * auth slice's queries are consumed by the shell.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { getPreferences, hasAccessToken, updatePreferences } from '@/api';
import type { UserPreferences, UserPreferencesUpdate } from '@/api';

export const preferencesKey = ['preferences'] as const;

/** The caller's preferences (default model). Only runs once authenticated. */
export function usePreferences() {
  return useQuery<UserPreferences>({
    queryKey: preferencesKey,
    queryFn: ({ signal }) => getPreferences(signal),
    enabled: hasAccessToken(),
    staleTime: 60_000,
  });
}

/** Set / clear the caller's preferences; primes the cache from the response. */
export function useUpdatePreferences() {
  const queryClient = useQueryClient();
  return useMutation<UserPreferences, unknown, UserPreferencesUpdate>({
    mutationFn: (body) => updatePreferences(body),
    onSuccess: (data) => queryClient.setQueryData(preferencesKey, data),
  });
}
