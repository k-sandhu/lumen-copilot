/**
 * System-status feature root. Demonstrates the state-coverage bar on the
 * skeleton: EVERY async state is implemented for the readiness query —
 * loading, error (actionable retry), empty, and success (each DependencyStatus
 * with ok/degraded) — plus the live WS indicator. This is the pattern the
 * product features (chat, citations) will reuse.
 */
import { useReadiness } from '../hooks/useReadiness';
import { DependencyRow } from './DependencyRow';
import { WsStatusIndicator } from './WsStatusIndicator';
import { Card, CardBody, CardHeader, CardTitle } from '@/components/Card';
import { ScrollArea } from '@/components/ScrollArea';
import { StatusBadge } from '@/components/StatusBadge';
import { ApiError } from '@/api';

export function SystemStatusPanel() {
  const query = useReadiness();

  return (
    <Card className="flex h-full flex-col">
      <CardHeader className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          <CardTitle>Backend status</CardTitle>
          <OverallBadge query={query} />
        </div>
        <WsStatusIndicator />
      </CardHeader>

      {/* min-h-0 lets the body shrink so its ScrollArea scrolls independently. */}
      <CardBody className="min-h-0 flex-1 p-0">
        <ScrollArea viewportClassName="px-4 py-3">
          <StatusBody query={query} />
        </ScrollArea>
      </CardBody>
    </Card>
  );
}

type ReadinessQuery = ReturnType<typeof useReadiness>;

function OverallBadge({ query }: { query: ReadinessQuery }) {
  if (query.isPending) return <StatusBadge tone="pending" pulse>Checking</StatusBadge>;
  if (query.isError) return <StatusBadge tone="danger">Unreachable</StatusBadge>;
  const ready = query.data?.status === 'ready';
  return (
    <StatusBadge tone={ready ? 'ok' : 'degraded'}>
      {ready ? 'Ready' : 'Degraded'}
    </StatusBadge>
  );
}

function StatusBody({ query }: { query: ReadinessQuery }) {
  // LOADING
  if (query.isPending) {
    return (
      <div role="status" aria-live="polite" className="space-y-2" aria-busy="true">
        <p className="text-sm text-foreground-muted">Checking backend readiness…</p>
        {[0, 1, 2].map((i) => (
          <div
            key={i}
            className="h-8 animate-pulse rounded bg-surface-muted"
            aria-hidden="true"
          />
        ))}
      </div>
    );
  }

  // ERROR — actionable retry, never a dead end.
  if (query.isError) {
    const message =
      query.error instanceof ApiError
        ? query.error.displayMessage
        : 'The backend could not be reached.';
    return (
      <div role="alert" className="space-y-3">
        <p className="text-sm font-medium text-danger">Couldn’t load backend status</p>
        <p className="text-sm text-foreground-muted">{message}</p>
        <button
          type="button"
          onClick={() => void query.refetch()}
          disabled={query.isFetching}
          className="rounded-md border border-border bg-surface px-3 py-1.5 text-sm hover:bg-surface-muted disabled:opacity-60"
        >
          {query.isFetching ? 'Retrying…' : 'Retry'}
        </button>
      </div>
    );
  }

  const deps = query.data.dependencies;

  // EMPTY — readiness with no dependencies reported.
  if (deps.length === 0) {
    return (
      <p className="text-sm text-foreground-muted">
        Backend is reachable but reported no dependencies.
      </p>
    );
  }

  // SUCCESS — list each dependency with its ok/degraded state.
  return (
    <div>
      {query.isFetching && (
        <p className="mb-2 text-xs text-foreground-muted" aria-live="polite">
          Refreshing…
        </p>
      )}
      <ul aria-label="Backend dependencies">
        {deps.map((dep) => (
          <DependencyRow key={dep.name} dep={dep} />
        ))}
      </ul>
    </div>
  );
}
