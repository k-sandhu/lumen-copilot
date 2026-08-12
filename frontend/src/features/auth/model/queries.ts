/**
 * Server-state hooks for auth — TanStack Query (NOT a store). The current user
 * is server data fetched from GET /auth/me; mutations (login/logout) invalidate
 * it. The coarse session status lives in the Zustand authStore.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { getCurrentUser, hasAccessToken, login, logout } from '@/api';
import type { CurrentUser, LoginRequest } from '@/api';
import { useEphemeralMutation } from '@/lib/useEphemeralMutation';
import { useAuthStore } from './authStore';

export const currentUserQueryKey = ['auth', 'me'] as const;

/**
 * The authenticated principal (AC-2). Only runs once a token is held — there is
 * no point calling /auth/me unauthenticated (it would just 401). Inherits the
 * shared QueryClient retry policy.
 */
export function useCurrentUser() {
  return useQuery<CurrentUser>({
    queryKey: currentUserQueryKey,
    queryFn: ({ signal }) => getCurrentUser(signal),
    enabled: hasAccessToken(),
    staleTime: 60_000,
  });
}

/**
 * Login mutation (AC-1). On success the token is already stored by `login()`;
 * we flip the session status and prime /auth/me so the shell renders the user
 * immediately. A bad-credentials 401 surfaces as the mutation error — the
 * screen shows a single generic message (AC-4, no account-existence leak).
 */
export function useLogin() {
  const queryClient = useQueryClient();
  const markAuthenticated = useAuthStore((s) => s.markAuthenticated);

  return useEphemeralMutation<void, unknown, LoginRequest>({
    mutationFn: async (credentials, { signal }) => {
      await login(credentials, signal);
    },
    onSuccess: () => {
      markAuthenticated();
      void queryClient.invalidateQueries({ queryKey: currentUserQueryKey });
    },
  });
}

/**
 * Logout mutation (AC-2). Revokes server-side, clears the token (which the
 * authStore observes → unauthenticated), and drops cached server state so no
 * other user's data lingers in the cache.
 */
export function useLogout() {
  const queryClient = useQueryClient();
  const markUnauthenticated = useAuthStore((s) => s.markUnauthenticated);

  return useMutation<void, unknown, void>({
    mutationFn: () => logout(),
    onSettled: () => {
      markUnauthenticated();
      queryClient.clear();
    },
  });
}
