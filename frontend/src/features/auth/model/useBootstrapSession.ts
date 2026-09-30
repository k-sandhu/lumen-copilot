/**
 * Boot-time session bootstrap. The access token is memory-only (token.ts), so a
 * fresh page load starts with no token. We attempt one silent refresh against
 * the httpOnly refresh cookie:
 *   - succeeds → token stored, status → authenticated (the user stays signed in
 *     across reloads)
 *   - fails    → status → unauthenticated (route guard sends them to /login)
 *
 * Runs exactly ONCE per page load. The guard is module-scoped (not a per-mount
 * ref) so React StrictMode's mount→unmount→remount in dev cannot start it twice
 * NOR strand it: the in-flight promise always lands a terminal status even if
 * the component that kicked it off has been remounted. Until it resolves the
 * status is `unknown` and the guard shows a loading state rather than flashing
 * the login screen (AC-3).
 */
import { useCallback, useEffect } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import {
  cancelInFlightRefresh,
  clearAccessToken,
  getAccessToken,
  getAuthIntentGeneration,
  isAuthIntentCurrent,
  refresh,
  registerRefreshHandler,
} from '@/api';
import { useAuthStore } from './authStore';
import { transitionPrincipal } from './principalTransition';

let bootstrapPromise: Promise<void> | null = null;

/** Attempt the one-time silent refresh; idempotent across callers/remounts. */
export function bootstrapSession(): Promise<void> {
  if (!bootstrapPromise) {
    bootstrapPromise = (async () => {
      const authIntentGeneration = getAuthIntentGeneration();
      try {
        await refresh();
        if (getAccessToken() !== null) {
          useAuthStore.getState().markAuthenticated();
        }
      } catch {
        if (getAccessToken() !== null) {
          // A newer login won while bootstrap was in flight. Its live token is
          // authoritative regardless of how the discarded bootstrap settled.
          useAuthStore.getState().markAuthenticated();
        } else if (isAuthIntentCurrent(authIntentGeneration)) {
          useAuthStore.getState().markUnauthenticated();
        }
      }
    })();
  }
  return bootstrapPromise;
}

/** Test-only: reset the one-time guard so each test bootstraps fresh. */
export function resetBootstrapForTests(): void {
  bootstrapPromise = null;
  registerRefreshHandler(null);
}

export function useBootstrapSession(): void {
  useEffect(() => {
    void bootstrapSession();
  }, []);
}

/** Explicit terminal recovery when the selected HttpOnly credential was lost. */
export function useSessionRecovery(): () => void {
  const queryClient = useQueryClient();
  return useCallback(() => {
    // Reserve a newer intent and clear the selector before aborting old work.
    // Even an uncooperative late response cannot restore the abandoned session.
    clearAccessToken();
    void cancelInFlightRefresh();
    // Bootstrap has no bearer, so token clearing alone emits no notification.
    transitionPrincipal(queryClient, 'unauthenticated');
  }, [queryClient]);
}
