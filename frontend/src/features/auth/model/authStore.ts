/**
 * Auth status — cross-cutting client-side UI state (Zustand), NOT server data.
 * The current user (CurrentUser) is server state and lives in TanStack Query
 * (see useCurrentUser); this store holds only the coarse session status the
 * route guard branches on (frontend/AGENTS.md: don't mirror server data here).
 *
 * Lifecycle:
 *   unknown          — bootstrapping; we are attempting a silent refresh
 *   authenticated    — a valid access token is held
 *   unauthenticated  — no session; the guard routes to /login
 *
 * The store SUBSCRIBES to the api token holder so that a token cleared anywhere
 * (a failed silent refresh, logout) deterministically routes back to login
 * (AC-4) without each call site having to remember to update status.
 */
import { create } from 'zustand';
import { subscribeToken } from '@/api';
import { clearCredentialDrafts } from '@/lib/credentialLifecycle';

export type AuthStatus = 'unknown' | 'authenticated' | 'unauthenticated';

interface AuthState {
  status: AuthStatus;
  markAuthenticated: () => void;
  markUnauthenticated: () => void;
}

export const useAuthStore = create<AuthState>((set) => ({
  status: 'unknown',
  markAuthenticated: () => {
    clearCredentialDrafts();
    set({ status: 'authenticated' });
  },
  markUnauthenticated: () => {
    clearCredentialDrafts();
    set({ status: 'unauthenticated' });
  },
}));

// When the access token is dropped (failed refresh / logout) flip to
// unauthenticated; when one appears, mark authenticated. Subscribed once at
// module load — the api/ holder is the single source of truth for "do we have a
// token", and the guard reacts to the derived status.
subscribeToken((token) => {
  const { status, markAuthenticated, markUnauthenticated } = useAuthStore.getState();
  if (token === null) {
    if (status !== 'unauthenticated') markUnauthenticated();
  } else if (status !== 'authenticated') {
    markAuthenticated();
  }
});
