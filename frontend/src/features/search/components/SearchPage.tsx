/**
 * SearchPage (#84) — the standalone `/search` page: the auth gate + app chrome
 * around the SearchScreen, kept INSIDE the feature slice so the route is
 * self-contained (ADR-0008 §1: a feature edits only its own files — no edit to
 * routes/). Mirrors the documents page shape: a guarded, full-height page reusing
 * the auth RouteGuard and account menu, a back-to-chat link and theme toggle.
 */
import { Link } from 'react-router-dom';
import { ErrorBoundary } from '@/components/ErrorBoundary';
import { CurrentUserMenu, RouteGuard } from '@/features/auth';
import { useUiStore } from '@/stores/ui';
import { SearchScreen } from './SearchScreen';

export function SearchPage() {
  return (
    <RouteGuard>
      <SearchShell />
    </RouteGuard>
  );
}

function SearchShell() {
  const theme = useUiStore((s) => s.theme);
  const toggleTheme = useUiStore((s) => s.toggleTheme);

  return (
    <div className="flex h-screen flex-col bg-surface text-foreground">
      <header className="flex shrink-0 items-center justify-between gap-3 border-b border-border px-4 py-3">
        <div className="flex items-center gap-2">
          <Link
            to="/"
            className="rounded-md border border-border px-2 py-1 text-sm hover:bg-surface-muted"
          >
            ← Chat
          </Link>
          <h1 className="text-sm font-semibold">Search</h1>
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

      {/* min-h-0 lets the screen own its own scroll regions (independent panes). */}
      <div className="min-h-0 flex-1">
        <ErrorBoundary label="Search">
          <SearchScreen />
        </ErrorBoundary>
      </div>
    </div>
  );
}
