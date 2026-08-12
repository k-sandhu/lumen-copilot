/**
 * Server-state hooks for auth — TanStack Query (NOT a store). The current user
 * is server data fetched from GET /auth/me; mutations (login/logout) invalidate
 * it. The coarse session status lives in the Zustand authStore.
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import {
  clearAccessToken,
  getAccessToken,
  getCurrentUser,
  hasAccessToken,
  login,
  logout,
} from '@/api';
import type { CurrentUser, LoginRequest } from '@/api';
import { useEphemeralMutation } from '@/lib/useEphemeralMutation';
import { useAuthStore } from './authStore';
import { transitionPrincipal } from './principalTransition';

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
 * Logout action (AC-2). The canonical principal lifecycle first drops every
 * client-side tenant/query holder; server revocation then runs best-effort with
 * the outgoing bearer and is deliberately not awaited by the UI.
 */
export function useLogout() {
  const queryClient = useQueryClient();
  const mounted = useRef(true);
  const pending = useRef(false);
  const [isPending, setIsPending] = useState(false);

  useEffect(
    () => () => {
      mounted.current = false;
    },
    [],
  );

  const mutate = useCallback(() => {
    if (pending.current) return;
    pending.current = true;
    setIsPending(true);

    const bearer = getAccessToken();
    // Synchronous with the click: blank/remask drafts, abort credential work,
    // clear every query/mutation, and leave the authenticated route before the
    // best-effort revocation is awaited.
    clearAccessToken();
    // An authenticated route normally has a bearer, so clearAccessToken's
    // synchronous notification already ran the canonical teardown. Keep the
    // null-bearer fallback idempotent for defensive/direct hook use without
    // firing registered form clearers twice during the normal unmount path.
    if (bearer === null) transitionPrincipal(queryClient, 'unauthenticated');

    void logout(bearer, false).finally(() => {
      pending.current = false;
      if (mounted.current) setIsPending(false);
    });
  }, [queryClient]);

  return { mutate, isPending };
}
