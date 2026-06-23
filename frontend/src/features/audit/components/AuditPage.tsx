/**
 * `/audit` — the standalone audit-log page (#86). Auth-gated by the shared
 * `RouteGuard` (the trail itself is further restricted to admin/security at the
 * api/ boundary — a `member` who reaches the page sees the 403 state, INV-5).
 * Reuses the shared `PageChrome` (back-to-app + theme toggle) so the slice owns
 * only its own files (ADR-0008 §1) and never edits `routes/`.
 */
import { ErrorBoundary } from '@/components/ErrorBoundary';
import { PageChrome } from '@/components/PageChrome';
import { RouteGuard } from '@/features/auth';
import { AuditPanel } from './AuditPanel';

export function AuditPage() {
  return (
    <RouteGuard>
      <PageChrome title="Audit log">
        <ErrorBoundary label="Audit log">
          <AuditPanel />
        </ErrorBoundary>
      </PageChrome>
    </RouteGuard>
  );
}
