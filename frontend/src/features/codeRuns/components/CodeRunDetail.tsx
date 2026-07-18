/**
 * CodeRunDetail (#232) — the standalone after-the-fact inspection view for one
 * code run, driven purely by GET /code-runs/{id} (no chat stream). Used for
 * debuggable-runs (E6-5): open a run by id and see exactly what the agent ran and
 * what it produced.
 *
 * Implements every read-endpoint state: loading skeleton, actionable error (with
 * honest 401/404 dead-ends, INV-1/INV-2/INV-4), and the populated record via the
 * shared CodeRunInspector. A non-terminal run polls (queries.ts refetchInterval)
 * so an in-progress run's status + captured output catch up.
 */
import { ApiError } from '@/api';
import { Icon } from '@/ui';
import { useCancelCodeRun, useCodeRun } from '../model/queries';
import { isTerminalCodeRun } from '../model/presentation';
import { viewFromRecord } from '../model/view';
import { CodeRunInspector } from './CodeRunInspector';

export interface CodeRunDetailProps {
  runId: string;
  /** Resolve an artifact id to a link (forwarded to the inspector). */
  resolveArtifactHref?: (artifactId: string) => string | undefined;
}

export function CodeRunDetail({ runId, resolveArtifactHref }: CodeRunDetailProps) {
  const query = useCodeRun(runId);
  const cancel = useCancelCodeRun(runId);

  if (query.isPending) {
    return (
      <div role="status" aria-live="polite" aria-busy="true" className="space-y-3 p-4">
        <span className="sr-only">Loading code run…</span>
        <div className="h-5 w-40 animate-pulse rounded bg-surface-muted" aria-hidden="true" />
        <div className="h-40 animate-pulse rounded bg-surface-muted" aria-hidden="true" />
        <div className="h-24 animate-pulse rounded bg-surface-muted" aria-hidden="true" />
      </div>
    );
  }

  if (query.isError) {
    const status = query.error instanceof ApiError ? query.error.status : 0;
    const deadEnd = status === 401 || status === 404;
    const message =
      status === 401
        ? 'Your session expired. Sign in again to continue.'
        : status === 404
          ? 'This code run no longer exists, or you don’t have access to it.'
          : query.error instanceof ApiError
            ? query.error.displayMessage || 'Something went wrong.'
            : 'Something went wrong.';
    return (
      <div role="alert" className="space-y-2 p-4 text-sm">
        <p className="flex items-center gap-2 font-medium text-danger">
          <Icon name="alert-triangle" className="shrink-0" />
          Couldn’t load this code run
        </p>
        <p className="text-foreground-muted">{message}</p>
        {!deadEnd ? (
          <button
            type="button"
            onClick={() => void query.refetch()}
            disabled={query.isFetching}
            className="rounded-md border border-border bg-surface px-3 py-1.5 hover:bg-surface-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-60"
          >
            {query.isFetching ? 'Retrying…' : 'Retry'}
          </button>
        ) : null}
      </div>
    );
  }

  const base = viewFromRecord(query.data);
  const live = query.isFetching && !isTerminalCodeRun(query.data.status);
  return (
    <div className="p-4">
      <CodeRunInspector
        view={{ ...base, streaming: base.streaming || live }}
        onCancel={() => cancel.mutate()}
        cancelling={cancel.isPending}
        {...(resolveArtifactHref ? { resolveArtifactHref } : {})}
      />
      {cancel.isError ? (
        <p role="alert" className="mt-2 text-xs text-danger">
          Couldn’t cancel this execution. Try resetting the chat sandbox.
        </p>
      ) : null}
    </div>
  );
}
