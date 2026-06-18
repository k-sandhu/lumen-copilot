import type { ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { useUiStore } from '@/stores/ui';

interface PageChromeProps {
  /** Page title shown in the slim top bar. */
  title: string;
  /** Right-aligned extra actions (e.g. a cross-link to the sibling page). */
  actions?: ReactNode;
  /** Optional left-side nav toggle (the docs viewer uses it on narrow screens). */
  onToggleNav?: () => void;
  navOpen?: boolean;
  children: ReactNode;
}

/**
 * Full-screen chrome for the standalone developer pages (`/docs`, `/features`) —
 * deliberately separate from the chat shell in App.tsx (the user asked for these
 * as pages "separate from the rest of the application"). Pinned top bar with a
 * back-to-app link + theme toggle; the body fills and scrolls within each page.
 */
export function PageChrome({ title, actions, onToggleNav, navOpen, children }: PageChromeProps) {
  const theme = useUiStore((s) => s.theme);
  const toggleTheme = useUiStore((s) => s.toggleTheme);

  return (
    <div className="flex h-screen flex-col bg-surface text-foreground">
      <header className="flex shrink-0 items-center justify-between gap-3 border-b border-border px-4 py-3">
        <div className="flex min-w-0 items-center gap-2">
          {onToggleNav ? (
            <button
              type="button"
              onClick={onToggleNav}
              aria-pressed={navOpen ?? false}
              aria-label={navOpen ? 'Hide navigation' : 'Show navigation'}
              className="rounded-md border border-border px-2 py-1 text-sm hover:bg-surface-muted md:hidden"
            >
              ☰
            </button>
          ) : null}
          <Link
            to="/"
            className="rounded-md border border-border px-2 py-1 text-sm hover:bg-surface-muted"
          >
            ← App
          </Link>
          <h1 className="truncate text-sm font-semibold">{title}</h1>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          {actions}
          <button
            type="button"
            onClick={toggleTheme}
            aria-label="Toggle color theme"
            className="rounded-md border border-border px-2 py-1 text-sm hover:bg-surface-muted"
          >
            {theme === 'dark' ? '☾ Dark' : '☀ Light'}
          </button>
        </div>
      </header>

      {/* min-h-0 is load-bearing: lets the body own its own scroll, not the page. */}
      <div className="flex min-h-0 flex-1 flex-col">{children}</div>
    </div>
  );
}
