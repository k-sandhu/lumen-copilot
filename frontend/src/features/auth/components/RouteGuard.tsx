/**
 * RouteGuard (AC-3) — the single auth gate around the app shell.
 *
 *   unknown          → loading state (silent refresh in flight; no login flash)
 *   unauthenticated  → <LoginScreen/>
 *   authenticated    → children (the app shell)
 *
 * Mounts `useBootstrapSession` so a page reload re-establishes the session from
 * the httpOnly refresh cookie before deciding. A failed silent refresh anywhere
 * in the app clears the token, which the authStore observes → unauthenticated →
 * this guard swaps back to the login screen (AC-4).
 */
import type { ReactNode } from 'react';
import { useAuthStore } from '../model/authStore';
import { useBootstrapSession } from '../model/useBootstrapSession';
import { LoginScreen } from './LoginScreen';

export function RouteGuard({ children }: { children: ReactNode }) {
  useBootstrapSession();
  const status = useAuthStore((s) => s.status);

  if (status === 'unknown') {
    return (
      <div
        role="status"
        aria-live="polite"
        className="flex min-h-screen items-center justify-center bg-surface text-sm text-foreground-muted"
      >
        Restoring your session…
      </div>
    );
  }

  if (status === 'unauthenticated') {
    return <LoginScreen />;
  }

  return <>{children}</>;
}
