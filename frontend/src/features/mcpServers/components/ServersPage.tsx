/**
 * ServersPage (#228) — the `/mcp-servers` screen body. The app shell owns the
 * chrome (brand + top bar + nav rail + theme + account) and the auth gate lives in
 * the layout route, so — exactly like Sources/Assistants — this nests through the
 * shell-aware `PageChrome` and renders ONLY its body inside the shell. It does not
 * render its own top bar, RouteGuard, or account menu.
 *
 * The slice owns only its own files (ADR-0008 §1) — no edit to routes/.
 */
import { ErrorBoundary } from '@/components/ErrorBoundary';
import { PageChrome } from '@/components/PageChrome';
import { ServersPanel } from './ServersPanel';

export function ServersPage() {
  return (
    <PageChrome title="MCP servers">
      <ErrorBoundary label="MCP servers">
        <ServersPanel />
      </ErrorBoundary>
    </PageChrome>
  );
}
