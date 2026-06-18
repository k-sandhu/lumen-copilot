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
import { useEffect } from 'react';
import { refresh } from '@/api';
import { useAuthStore } from './authStore';

let bootstrapPromise: Promise<void> | null = null;

/** Attempt the one-time silent refresh; idempotent across callers/remounts. */
export function bootstrapSession(): Promise<void> {
  if (!bootstrapPromise) {
    bootstrapPromise = (async () => {
      try {
        await refresh();
        useAuthStore.getState().markAuthenticated();
      } catch {
        useAuthStore.getState().markUnauthenticated();
      }
    })();
  }
  return bootstrapPromise;
}

/** Test-only: reset the one-time guard so each test bootstraps fresh. */
export function resetBootstrapForTests(): void {
  bootstrapPromise = null;
}

export function useBootstrapSession(): void {
  useEffect(() => {
    void bootstrapSession();
  }, []);
}
