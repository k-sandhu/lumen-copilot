/**
 * Chat screen element for `/` (issue #110). The app shell (brand + top bar + nav
 * rail) and the auth gate now live in the LAYOUT route (`routes/router.tsx`), so
 * this is just the chat workspace that renders into the shell's main `<Outlet/>`.
 *
 * The chat slice (`ChatView`) owns its own multi-pane layout (history sidebar +
 * conversation + composer + citation viewer), each pane independently scrollable
 * inside the shell main (`min-h-0` grid cell) — frontend/AGENTS.md "Independently
 * scrollable panes". The previous bare header, theme toggle, and floating
 * NavOverlay are gone: the shell supersedes them.
 */
import { ErrorBoundary } from '@/components/ErrorBoundary';
import { ChatView } from '@/features/chat';

/** Route element for `/` — the chat workspace inside the app shell. */
export function App() {
  return (
    <div className="flex h-full min-h-0 flex-1 bg-surface text-foreground">
      <ErrorBoundary label="Chat">
        <ChatView />
      </ErrorBoundary>
    </div>
  );
}
