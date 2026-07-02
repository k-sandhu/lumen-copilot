/**
 * RunsPage (#237) — the body for the `/runs/*` surface (the run inbox + detail).
 * Contributes ONE route (`/runs/*`) and resolves its product paths with a nested
 * <Routes>:
 *
 *   /runs        → the run history list
 *   /runs/:id    → the run detail (transcript, citations, outputs, error)
 *   anything else → back to the inbox (no blank pane)
 *
 * An ErrorBoundary around the feature root keeps a render crash from wedging the
 * app (frontend/AGENTS.md "Resilient").
 */
import { Navigate, Route, Routes, useParams } from 'react-router-dom';
import { ErrorBoundary } from '@/components/ErrorBoundary';
import { PageChrome } from '@/components/PageChrome';
import { RunHistory } from './RunHistory';
import { RunDetail } from './RunDetail';

export function RunsPage() {
  return (
    <PageChrome title="Run history">
      <ErrorBoundary label="Runs">
        <Routes>
          <Route index element={<RunHistory />} />
          <Route path=":runId" element={<DetailRoute />} />
          <Route path="*" element={<Navigate to="/runs" replace />} />
        </Routes>
      </ErrorBoundary>
    </PageChrome>
  );
}

/** Reads the `:runId` param and hands it to the detail view. */
function DetailRoute() {
  const { runId } = useParams<{ runId: string }>();
  if (!runId) return <Navigate to="/runs" replace />;
  return <RunDetail runId={runId} />;
}
