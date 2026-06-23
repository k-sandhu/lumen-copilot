/**
 * SourcesPage (#27) — the `/sources` screen body. The app shell (issue #110) owns
 * the chrome (brand + top bar + nav rail + theme + account) and the auth gate
 * lives in the layout route, so — exactly like Audit/Admin/Search — this nests
 * through the shell-aware `PageChrome` and renders ONLY its body inside the shell.
 * It does not render its own top bar, RouteGuard, or account menu.
 *
 * The slice owns only its own files (ADR-0008 §1) — no edit to routes/.
 */
import { ErrorBoundary } from '@/components/ErrorBoundary';
import { PageChrome } from '@/components/PageChrome';
import { SourcesPanel } from './SourcesPanel';

export function SourcesPage() {
  return (
    <PageChrome title="Sources">
      <ErrorBoundary label="Sources">
        <SourcesPanel />
      </ErrorBoundary>
    </PageChrome>
  );
}
