import type { ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { useUiStore } from '@/stores/ui';
import { useInAppShell } from '@/routes/shell/ShellContext';

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
 * Chrome for standalone pages. When rendered INSIDE the app shell (issue #110)
 * the shell already provides brand + top bar + nav rail, so this suppresses its
 * own header/back-link/theme-toggle and renders just the scrollable body — screens
 * nest cleanly with no duplicate chrome. Standalone (a dev page `/docs`/`/features`
 * reached directly, outside the shell) it keeps the full pinned top bar with a
 * back-to-app link + theme toggle, as before.
 */
export function PageChrome({ title, actions, onToggleNav, navOpen, children }: PageChromeProps) {
  const theme = useUiStore((s) => s.theme);
  const toggleTheme = useUiStore((s) => s.toggleTheme);
  const inShell = useInAppShell();

  // Inside the shell the chrome is redundant — render only the body, filling the
  // shell's main and owning its own scroll (min-h-0 contained overflow).
  if (inShell) {
    return (
      <div className="flex min-h-0 flex-1 flex-col bg-surface text-foreground">{children}</div>
    );
  }

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
