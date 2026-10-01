import { useLayoutEffect, type ReactNode } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { getAccessToken, subscribeToken, type TokenChangeReason } from '@/api';
import { useAuthStore } from './authStore';
import { transitionPrincipal } from './principalTransition';

function isSamePrincipalRefresh(
  previousToken: string | null,
  token: string | null,
  reason: TokenChangeReason,
): boolean {
  // A refresh reason is emitted only by the generation-checked refresh commit
  // in token.ts; non-null values alone are intentionally insufficient.
  return previousToken !== null && token !== null && reason === 'refresh';
}

/** Bind token transitions to the QueryClient owned by the surrounding provider. */
export function PrincipalLifecycle({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();

  useLayoutEffect(() => {
    let previousToken = getAccessToken();
    return subscribeToken((token, reason) => {
      const samePrincipalRefresh = isSamePrincipalRefresh(previousToken, token, reason);
      previousToken = token;

      if (samePrincipalRefresh) {
        useAuthStore.getState().markAuthenticated();
        return;
      }

      transitionPrincipal(queryClient, token === null ? 'unauthenticated' : 'authenticated');
    });
  }, [queryClient]);

  return <>{children}</>;
}
