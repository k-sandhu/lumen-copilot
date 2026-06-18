/**
 * `/documents` — the collections + documents management view (#49). A guarded,
 * standalone page (auth-gated like the chat shell) that gives the documents slice
 * a full-height home until it's folded into the chat workspace. Reuses the app
 * chrome (header, theme toggle, account menu) and the auth RouteGuard from #48.
 */
import { Link } from 'react-router-dom';
import { ErrorBoundary } from '@/components/ErrorBoundary';
import { CurrentUserMenu, RouteGuard } from '@/features/auth';
import { DocumentsPanel } from '@/features/documents';
import { useUiStore } from '@/stores/ui';

export function DocumentsRoute() {
  return (
    <RouteGuard>
      <DocumentsShell />
    </RouteGuard>
  );
}

function DocumentsShell() {
  const theme = useUiStore((s) => s.theme);
  const toggleTheme = useUiStore((s) => s.toggleTheme);

  return (
    <div className="flex h-screen flex-col bg-surface text-foreground">
      <header className="flex shrink-0 items-center justify-between gap-3 border-b border-border px-4 py-3">
        <div className="flex items-center gap-2">
          <Link to="/" className="rounded-md border border-border px-2 py-1 text-sm hover:bg-surface-muted">
            ← Chat
          </Link>
          <h1 className="text-sm font-semibold">Documents</h1>
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

      {/* min-h-0 lets the panel own its own scroll regions (independent panes). */}
      <div className="min-h-0 flex-1">
        <ErrorBoundary label="Documents">
          <DocumentsPanel />
        </ErrorBoundary>
      </div>
    </div>
  );
}
