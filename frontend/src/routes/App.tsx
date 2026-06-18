/**
 * Two-pane app shell — establishes the multi-pane scroll pattern the chat UI
 * will reuse (frontend/AGENTS.md "Independently scrollable panes").
 *
 * Layout contract (verify the CSS is REAL):
 *   - Outer: `h-screen` column flex; the header is pinned (shrink-0).
 *   - Middle row: flex, `min-h-0` so children may shrink below content height.
 *   - Left rail + main pane each own their overflow via <ScrollArea> (h-full).
 *   - A composer area stays pinned at the bottom of the MAIN pane (shrink-0),
 *     while the content above it scrolls — the chat layout in miniature.
 * Nothing forces a whole-page scroll; long content stays inside its pane.
 */
import { MarkdownView } from '@/lib/markdown';
import { ScrollArea } from '@/components/ScrollArea';
import { ErrorBoundary } from '@/components/ErrorBoundary';
import { NavOverlay } from '@/components/NavOverlay';
import { SystemStatusPanel } from '@/features/system-status';
import { useUiStore } from '@/stores/ui';
import { cn } from '@/lib/cn';
import { WELCOME_NOTE } from './welcomeNote';

export function App() {
  const railCollapsed = useUiStore((s) => s.railCollapsed);
  const toggleRail = useUiStore((s) => s.toggleRail);
  const theme = useUiStore((s) => s.theme);
  const toggleTheme = useUiStore((s) => s.toggleTheme);

  return (
    <div className="flex h-screen flex-col bg-surface text-foreground">
      {/* Pinned header */}
      <header className="flex shrink-0 items-center justify-between gap-3 border-b border-border px-4 py-3">
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={toggleRail}
            aria-pressed={railCollapsed}
            aria-label={railCollapsed ? 'Expand rail' : 'Collapse rail'}
            className="rounded-md border border-border px-2 py-1 text-sm hover:bg-surface-muted"
          >
            {railCollapsed ? '☰' : '⟨'}
          </button>
          <h1 className="text-sm font-semibold">Lumen Copilot</h1>
          <span className="rounded bg-surface-muted px-1.5 py-0.5 text-xs text-foreground-muted">
            skeleton
          </span>
        </div>
        <button
          type="button"
          onClick={toggleTheme}
          aria-label="Toggle color theme"
          className="rounded-md border border-border px-2 py-1 text-sm hover:bg-surface-muted"
        >
          {theme === 'dark' ? '☾ Dark' : '☀ Light'}
        </button>
      </header>

      {/* Middle row — min-h-0 is the load-bearing rule for independent scroll. */}
      <div className="flex min-h-0 flex-1">
        {/* Left rail — independently scrollable */}
        <aside
          className={cn(
            'min-h-0 shrink-0 border-r border-border transition-[width]',
            railCollapsed ? 'w-0 overflow-hidden' : 'w-72',
          )}
          aria-hidden={railCollapsed}
        >
          <ScrollArea viewportClassName="p-4">
            <ErrorBoundary label="Welcome note">
              <MarkdownView>{WELCOME_NOTE}</MarkdownView>
            </ErrorBoundary>
          </ScrollArea>
        </aside>

        {/* Main pane — content scrolls, composer stays pinned */}
        <main className="flex min-h-0 min-w-0 flex-1 flex-col">
          <div className="min-h-0 flex-1 p-4">
            <ErrorBoundary label="System status">
              <SystemStatusPanel />
            </ErrorBoundary>
          </div>

          {/* Pinned composer area (placeholder — the chat input lands here). */}
          <div className="shrink-0 border-t border-border p-4">
            <form
              className="flex items-center gap-2"
              onSubmit={(e) => e.preventDefault()}
              aria-label="Composer (disabled in skeleton)"
            >
              <input
                type="text"
                disabled
                placeholder="Chat composer lands here — disabled in the skeleton"
                className="min-w-0 flex-1 rounded-md border border-border bg-surface-muted px-3 py-2 text-sm text-foreground-muted"
                aria-label="Message"
              />
              <button
                type="submit"
                disabled
                className="rounded-md border border-border px-3 py-2 text-sm text-foreground-muted"
              >
                Send
              </button>
            </form>
          </div>
        </main>
      </div>

      {/* Floating links to the standalone developer pages (docs + features). */}
      <NavOverlay
        items={[
          { to: '/docs', label: 'Documentation', icon: '📖' },
          { to: '/features', label: 'Features built', icon: '✨' },
        ]}
      />
    </div>
  );
}
