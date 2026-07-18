/**
 * CodeRunPanel (#232) — the collapsible inline code-run surface for a chat turn.
 * Given a runId (and, while a run is live on the chat stream, its folded
 * `CodeRunActivity`), it fetches the full record from GET /code-runs/{id} and
 * merges the two so the panel is LIVE during the run (streamed stdout/stderr from
 * the WS) then settles on the durable record (code, resource usage, image digest).
 *
 * All async states of the read endpoint are handled: while a live activity is
 * present the panel renders that immediately (no blank wait); a read-endpoint
 * error with no live fallback surfaces an actionable retry (404 = existence
 * non-disclosure, no pointless retry); a lone id with neither is a labelled empty.
 *
 * The code record is fetched lazily — only once the panel is expanded — so a chat
 * with many runs doesn't fan out N reads on load. A live/non-terminal run keeps
 * polling via the query hook.
 */
import { useId, useState } from 'react';
import { ApiError } from '@/api';
import { Icon, StatusDot } from '@/ui';
import type { CodeRunActivity } from '@/features/chat/model/streamReducer';
import { useCancelCodeRun, useCodeRun } from '../model/queries';
import {
  CODE_RUN_STATUS_HEADLINE,
  CODE_RUN_STATUS_TONE,
  isTerminalCodeRun,
} from '../model/presentation';
import { mergeView, viewFromActivity, viewFromRecord, type CodeRunView } from '../model/view';
import { CodeRunInspector } from './CodeRunInspector';

export interface CodeRunPanelProps {
  runId: string;
  /** The live-stream activity for this run, while it is in flight (optional). */
  activity?: CodeRunActivity;
  /** Resolve an artifact id to a link (forwarded to the inspector). */
  resolveArtifactHref?: (artifactId: string) => string | undefined;
  /** Start expanded (a live/in-flight run defaults open so its output is visible). */
  defaultOpen?: boolean;
}

export function CodeRunPanel({
  runId,
  activity,
  resolveArtifactHref,
  defaultOpen,
}: CodeRunPanelProps) {
  const liveInFlight = activity ? !isTerminalCodeRun(activity.status) : false;
  const [open, setOpen] = useState(defaultOpen ?? liveInFlight);
  const bodyId = useId();
  const headingId = useId();

  // Fetch the full record only once expanded (lazy — avoid N reads on chat load).
  // A live run also polls; a terminal record stops (queries.ts refetchInterval).
  const query = useCodeRun(runId, { enabled: open });

  const headerStatus = activity?.status ?? query.data?.status ?? 'queued';
  const headerTone = CODE_RUN_STATUS_TONE[headerStatus];

  return (
    <div className="my-2 overflow-hidden rounded-lg border border-border bg-surface">
      <button
        type="button"
        className="flex w-full items-center gap-2 px-3 py-2 text-left text-sm hover:bg-surface-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent"
        aria-expanded={open}
        aria-controls={bodyId}
        onClick={() => setOpen((v) => !v)}
      >
        <Icon
          name="chevron-down"
          className={`shrink-0 transition-transform ${open ? '' : '-rotate-90'}`}
        />
        <StatusDot tone={headerTone} title={CODE_RUN_STATUS_HEADLINE[headerStatus]} />
        <span className="font-medium">Code run</span>
        <span className="truncate text-xs text-foreground-muted">
          {CODE_RUN_STATUS_HEADLINE[headerStatus]}
        </span>
      </button>

      {open ? (
        <div id={bodyId} className="border-t border-border px-3 py-3">
          <PanelBody
            runId={runId}
            activity={activity}
            headingId={headingId}
            query={query}
            resolveArtifactHref={resolveArtifactHref}
          />
        </div>
      ) : null}
    </div>
  );
}

function PanelBody({
  runId,
  activity,
  headingId,
  query,
  resolveArtifactHref,
}: {
  runId: string;
  activity: CodeRunActivity | undefined;
  headingId: string;
  query: ReturnType<typeof useCodeRun>;
  resolveArtifactHref?: (artifactId: string) => string | undefined;
}) {
  const cancel = useCancelCodeRun(runId);
  // Prefer the merged view when both live activity and the fetched record exist;
  // otherwise use whichever we have. This guarantees a non-blank pane the instant
  // the panel opens on a live run, even before the first read resolves.
  let view: CodeRunView | null = null;
  if (activity && query.data) view = mergeView(activity, query.data);
  else if (query.data) view = viewFromRecord(query.data);
  else if (activity) view = viewFromActivity(activity);

  // No live activity and the read is still pending → a skeleton (never blank).
  if (!view && query.isPending) {
    return (
      <div role="status" aria-live="polite" aria-busy="true" className="space-y-2">
        <span className="sr-only">Loading code run…</span>
        <div className="h-4 w-24 animate-pulse rounded bg-surface-muted" aria-hidden="true" />
        <div className="h-24 animate-pulse rounded bg-surface-muted" aria-hidden="true" />
      </div>
    );
  }

  // No live activity and the read failed → an actionable error (or an honest
  // existence-non-disclosure dead-end for a 404 / 401).
  if (!view && query.isError) {
    return (
      <ReadError error={query.error} onRetry={() => void query.refetch()} busy={query.isFetching} />
    );
  }

  if (!view) {
    return (
      <p className="px-1 py-6 text-center text-sm text-foreground-muted">
        This code run has no details to show.
      </p>
    );
  }

  return (
    <div className="space-y-2">
      <CodeRunInspector
        view={view}
        headingId={headingId}
        onCancel={() => cancel.mutate()}
        cancelling={cancel.isPending}
        {...(resolveArtifactHref ? { resolveArtifactHref } : {})}
      />
      {cancel.isError ? (
        <p role="alert" className="text-xs text-danger">
          Couldn’t cancel this execution. Try resetting the chat sandbox.
        </p>
      ) : null}
      {/* If we have a live view but the record read failed, note it inline without
          hiding the live output we already have (degraded, not dead). */}
      {activity && query.isError && !query.data ? (
        <p className="text-xs text-foreground-muted">
          Couldn’t load the full record for this run — showing the live output only.
        </p>
      ) : null}
    </div>
  );
}

/** The read-endpoint error surface — honest 401/404 messaging, retry otherwise. */
function ReadError({
  error,
  onRetry,
  busy,
}: {
  error: unknown;
  onRetry: () => void;
  busy: boolean;
}) {
  const status = error instanceof ApiError ? error.status : 0;
  const deadEnd = status === 401 || status === 404;
  const message =
    status === 401
      ? 'Your session expired. Sign in again to continue.'
      : status === 404
        ? 'This code run no longer exists, or you don’t have access to it.'
        : error instanceof ApiError
          ? error.displayMessage || 'Something went wrong loading this code run.'
          : 'Something went wrong loading this code run.';
  return (
    <div role="alert" className="space-y-2 py-2 text-sm">
      <p className="flex items-center gap-2 font-medium text-danger">
        <Icon name="alert-triangle" className="shrink-0" />
        Couldn’t load this code run
      </p>
      <p className="text-foreground-muted">{message}</p>
      {!deadEnd ? (
        <button
          type="button"
          onClick={onRetry}
          disabled={busy}
          className="rounded-md border border-border bg-surface px-3 py-1.5 hover:bg-surface-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-60"
        >
          {busy ? 'Retrying…' : 'Retry'}
        </button>
      ) : null}
    </div>
  );
}
