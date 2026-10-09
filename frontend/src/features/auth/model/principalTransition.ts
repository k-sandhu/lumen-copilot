import type { QueryClient } from '@tanstack/react-query';
import { clearCredentialDrafts } from '@/lib/credentialLifecycle';
import { useAuthStore, type AuthStatus } from './authStore';

/**
 * The one fail-closed client boundary for a principal transition. Cancellation
 * is initiated first (which synchronously aborts query signals), credential
 * holders are destroyed/aborted next, and both TanStack caches are then emptied
 * before the route status can render the next principal.
 */
export function transitionPrincipal(queryClient: QueryClient, status: AuthStatus): void {
  void queryClient.cancelQueries();
  clearCredentialDrafts();
  queryClient.getMutationCache().clear();
  queryClient.getQueryCache().clear();

  const auth = useAuthStore.getState();
  if (status === 'authenticated') auth.markAuthenticated();
  else if (status === 'unauthenticated') auth.markUnauthenticated();
  else useAuthStore.setState({ status: 'unknown' });
}
