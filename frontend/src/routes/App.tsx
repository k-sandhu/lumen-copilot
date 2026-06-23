/**
 * App shell — header (theme + account) over the chat workspace (issue #50). The
 * chat slice (`ChatView`) owns its own multi-pane layout (history sidebar +
 * conversation + composer + citation viewer), each pane independently scrollable
 * inside the `min-h-0` middle row (frontend/AGENTS.md "Independently scrollable
 * panes"). The auth guard (#48) still gates the whole shell.
 */
import { ErrorBoundary } from '@/components/ErrorBoundary';
import { NavOverlay } from '@/components/NavOverlay';
import { ChatView } from '@/features/chat';
import { CurrentUserMenu, RouteGuard } from '@/features/auth';
import { useUiStore } from '@/stores/ui';
import { featureNavItems } from './discovery';

/**
 * Route root for `/`. The auth guard gates the shell: unauthenticated → login;
 * authenticated → the chat workspace; bootstrapping → a loading state.
 */
export function App() {
  return (
    <RouteGuard>
      <AppShell />
    </RouteGuard>
  );
}

function AppShell() {
  const theme = useUiStore((s) => s.theme);
  const toggleTheme = useUiStore((s) => s.toggleTheme);

  return (
    <div className="flex h-screen flex-col bg-surface text-foreground">
      {/* Pinned header */}
      <header className="flex shrink-0 items-center justify-between gap-3 border-b border-border px-4 py-3">
        <div className="flex items-center gap-2">
          <h1 className="text-sm font-semibold">Lumen Copilot</h1>
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={toggleTheme}
            aria-label="Toggle color theme"
            className="rounded-md border border-border px-2 py-1 text-sm hover:bg-surface-muted"
          >
            {theme === 'dark' ? '☾ Dark' : '☀ Light'}
          </button>
          <ErrorBoundary label="Account menu">
            <CurrentUserMenu />
          </ErrorBoundary>
        </div>
      </header>

      {/* Middle row — min-h-0 is the load-bearing rule for independent scroll. */}
      <div className="flex min-h-0 flex-1">
        <ErrorBoundary label="Chat">
          <ChatView />
        </ErrorBoundary>
      </div>

      {/* Floating links to the standalone pages — assembled from each feature's
          own nav.ts via import.meta.glob (ADR-0008 §3), not a hand-edited list. */}
      <NavOverlay items={featureNavItems} />
    </div>
  );
}
