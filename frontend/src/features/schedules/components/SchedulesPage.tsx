/**
 * SchedulesPage (#237) — the body for the `/schedules/*` surface. Like the
 * assistants slice, it contributes ONE route (`/schedules/*`) to the
 * auto-discovered manifest and resolves its product paths with a nested <Routes>:
 *
 *   /schedules        → the schedule list
 *   /schedules/new    → the create form
 *   /schedules/:id    → the edit form
 *   anything else      → back to the list (no blank pane)
 *
 * An ErrorBoundary around the feature root keeps a render crash from wedging the
 * app (frontend/AGENTS.md "Resilient").
 */
import { Navigate, Route, Routes, useParams } from 'react-router-dom';
import { ErrorBoundary } from '@/components/ErrorBoundary';
import { PageChrome } from '@/components/PageChrome';
import { ScheduleList } from './ScheduleList';
import { ScheduleEditor } from './ScheduleEditor';

export function SchedulesPage() {
  return (
    <PageChrome title="Schedules">
      <ErrorBoundary label="Schedules">
        <Routes>
          <Route index element={<ScheduleList />} />
          <Route path="new" element={<ScheduleEditor scheduleId={null} />} />
          <Route path=":scheduleId" element={<EditorRoute />} />
          <Route path="*" element={<Navigate to="/schedules" replace />} />
        </Routes>
      </ErrorBoundary>
    </PageChrome>
  );
}

/** Reads the `:scheduleId` param and hands it to the editor. */
function EditorRoute() {
  const { scheduleId } = useParams<{ scheduleId: string }>();
  return <ScheduleEditor scheduleId={scheduleId ?? null} />;
}
