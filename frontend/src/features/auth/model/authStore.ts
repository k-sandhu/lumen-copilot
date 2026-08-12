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
 * `PrincipalLifecycle` subscribes to the api token holder and updates this
 * store only after it has cancelled requests and cleared the QueryClient. The
 * store deliberately remains a small status holder; it does not own server
 * state or a second QueryClient.
 */
import { create } from 'zustand';

export type AuthStatus = 'unknown' | 'authenticated' | 'unauthenticated';

interface AuthState {
  status: AuthStatus;
  markAuthenticated: () => void;
  markUnauthenticated: () => void;
}

export const useAuthStore = create<AuthState>((set) => ({
  status: 'unknown',
  markAuthenticated: () => set({ status: 'authenticated' }),
  markUnauthenticated: () => set({ status: 'unauthenticated' }),
}));
