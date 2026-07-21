/**
 * RunsPage (#237, #238) — the body for the `/runs/*` surface (the run inbox +
 * history + detail). Contributes ONE route (`/runs/*`) and resolves its product
 * paths with a nested <Routes>:
 *
 *   /runs         → the run history list
 *   /runs/inbox   → the in-app run inbox (delivered completed runs, #238)
 *   /runs/:id     → the run detail (transcript, citations, outputs, error)
 *   anything else → back to the history (no blank pane)
 *
 * An ErrorBoundary around the feature root keeps a render crash from wedging the
 * app (frontend/AGENTS.md "Resilient").
 */
import { NavLink, Navigate, Route, Routes, useParams } from 'react-router-dom';
import { ErrorBoundary } from '@/components/ErrorBoundary';
import { PageChrome } from '@/components/PageChrome';
import { RunHistory } from './RunHistory';
import { RunInbox } from './RunInbox';
import { RunDetail } from './RunDetail';

export function RunsPage() {
  return (
    <PageChrome title="Runs">
      <ErrorBoundary label="Runs">
        <Routes>
          <Route index element={<TabbedRuns tab="history" />} />
          <Route path="inbox" element={<TabbedRuns tab="inbox" />} />
          <Route path=":runId" element={<DetailRoute />} />
          <Route path="*" element={<Navigate to="/runs" replace />} />
        </Routes>
      </ErrorBoundary>
    </PageChrome>
  );
}

/** The two top-level run views (history + inbox) behind a shared tab strip. */
function TabbedRuns({ tab }: { tab: 'history' | 'inbox' }) {
  return (
    <div className="flex h-full min-h-0 flex-col">
      <nav aria-label="Runs views" className="flex shrink-0 gap-1 border-b border-border px-4 pt-2">
        <RunsTab to="/runs" end label="History" />
        <RunsTab to="/runs/inbox" label="Inbox" />
      </nav>
      <div className="min-h-0 flex-1">{tab === 'inbox' ? <RunInbox /> : <RunHistory />}</div>
    </div>
  );
}

function RunsTab({ to, label, end }: { to: string; label: string; end?: boolean }) {
  return (
    <NavLink
      to={to}
      end={end}
      className={({ isActive }) =>
        `rounded-t-md border-b-2 px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent ${
          isActive
            ? 'border-accent font-medium text-foreground'
            : 'border-transparent text-foreground-muted hover:text-foreground'
        }`
      }
    >
      {label}
    </NavLink>
  );
}

/** Reads the `:runId` param and hands it to the detail view. */
function DetailRoute() {
  const { runId } = useParams<{ runId: string }>();
  if (!runId) return <Navigate to="/runs" replace />;
  return <RunDetail runId={runId} />;
}
