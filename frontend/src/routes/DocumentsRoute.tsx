/**
 * `/documents` screen element (issue #110) — the collections + documents
 * management view (#49). The app shell (chrome) and the auth gate now live in the
 * LAYOUT route (`routes/router.tsx`), so this is just the documents panel that
 * renders into the shell's main `<Outlet/>`. The previous standalone header,
 * back-to-chat link, and theme toggle are gone — the shell supersedes them.
 *
 * `min-h-0` lets the panel own its own scroll regions (independent panes) inside
 * the shell main.
 */
import { ErrorBoundary } from '@/components/ErrorBoundary';
import { DocumentsPanel } from '@/features/documents';

export function DocumentsRoute() {
  return (
    <div className="min-h-0 flex-1 bg-surface text-foreground">
      <ErrorBoundary label="Documents">
        <DocumentsPanel />
      </ErrorBoundary>
    </div>
  );
}
