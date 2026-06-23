/**
 * `/admin` — the read-only admin console (#88, ADR-0007 §4). Composes the three
 * governance surfaces — Members & roles, Model governance, and the Approvals &
 * risk-tier map — each a self-contained panel that owns its own loading / empty /
 * error states (frontend/AGENTS.md: every state, not just success). The page body
 * scrolls independently of the pinned chrome (min-h-0 + contained overflow).
 *
 * Read-mostly for v1: there are deliberately NO mutating controls anywhere on
 * this screen. Each panel reads admin-only, tenant-scoped data through the api/
 * boundary; a non-admin caller (403, INV-5) or an expired session (401, INV-4)
 * sees an actionable per-panel error rather than a blank screen.
 */
import { PageChrome } from '@/components/PageChrome';
import { ScrollArea } from '@/components/ScrollArea';
import { ErrorBoundary } from '@/components/ErrorBoundary';
import { MembersPanel } from './MembersPanel';
import { ModelGovernancePanel } from './ModelGovernancePanel';
import { RiskTierPanel } from './RiskTierPanel';

export function AdminPage() {
  return (
    <PageChrome title="Admin">
      <ScrollArea viewportClassName="px-6 py-6">
        <div className="mx-auto flex max-w-4xl flex-col gap-6">
          <header>
            <h1 className="text-lg font-semibold text-foreground">Administration</h1>
            <p className="mt-1 text-sm text-foreground-muted">
              Read-only governance for this tenant. Editing members, model policy, and approval
              tiers is gated behind read-before-write controls and is not available here in v1.
            </p>
          </header>
          <ErrorBoundary label="Members">
            <MembersPanel />
          </ErrorBoundary>
          <ErrorBoundary label="Model governance">
            <ModelGovernancePanel />
          </ErrorBoundary>
          <ErrorBoundary label="Risk tiers">
            <RiskTierPanel />
          </ErrorBoundary>
        </div>
      </ScrollArea>
    </PageChrome>
  );
}
