/**
 * AssistantsPage (#212) — the body for the `/assistants/*` surface. The app shell
 * (issue #110) owns the chrome (brand + top bar + nav rail); like Sources/Admin,
 * this nests through the shell-aware `PageChrome` and renders only its body.
 *
 * The slice contributes ONE route (`/assistants/*`) to the auto-discovered manifest
 * (routes/discovery.ts scans one `route.tsx` per feature), so the three product
 * paths are resolved by a nested <Routes> here:
 *
 *   /assistants          → the library grid
 *   /assistants/describe → the conversational builder ("Describe your assistant", #213)
 *   /assistants/new      → the builder (create, blank form)
 *   /assistants/:id      → the builder (edit)
 *   anything else        → back to the library (no blank pane)
 *
 * An ErrorBoundary around the feature root keeps a render crash from wedging the
 * app (frontend/AGENTS.md "Resilient").
 */
import { Navigate, Route, Routes, useParams } from 'react-router-dom';
import { ErrorBoundary } from '@/components/ErrorBoundary';
import { PageChrome } from '@/components/PageChrome';
import { AssistantLibrary } from './AssistantLibrary';
import { AssistantEditor } from './AssistantEditor';
import { DescribeAssistant } from './DescribeAssistant';

export function AssistantsPage() {
  return (
    <PageChrome title="Assistants">
      <ErrorBoundary label="Assistants">
        <Routes>
          <Route index element={<AssistantLibrary />} />
          <Route path="describe" element={<DescribeAssistant />} />
          <Route path="new" element={<AssistantEditor assistantId={null} />} />
          <Route path=":assistantId" element={<EditorRoute />} />
          <Route path="*" element={<Navigate to="/assistants" replace />} />
        </Routes>
      </ErrorBoundary>
    </PageChrome>
  );
}

/** Reads the `:assistantId` param and hands it to the editor. */
function EditorRoute() {
  const { assistantId } = useParams<{ assistantId: string }>();
  return <AssistantEditor assistantId={assistantId ?? null} />;
}
