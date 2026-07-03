/**
 * VersionHistory (#214, F-AB-4, E6-7) — the append-only version-history panel in
 * the assistant editor. Lists the assistant's immutable published versions
 * (newest first, paginated), marks the current/published head, shows each
 * version's key config (instructions / model / tools / scope / autonomy), and a
 * lightweight FIELD-LEVEL diff vs. the current head. Each historical version
 * offers a Rollback action behind a ConfirmDialog explaining that rolling back
 * creates a NEW version equal to the chosen one — history is never mutated
 * (ADR-0011 §1).
 *
 * Server state via TanStack Query (queries.ts): the versions list is an infinite
 * query ("Load more"); a rollback invalidates versions + the assistant so the
 * panel and the editor header catch up. Every async surface has its own state:
 *   • LOADING skeleton while the first page resolves;
 *   • EMPTY ("not published yet") for a never-published draft;
 *   • ERROR + retry for a transient failure (a 404 = no access, honestly stated);
 *   • SUCCESS: the version cards, the current one badged.
 *
 * A11y: the current version is announced (aria-current + a visible badge); the
 * rollback confirm is the focus-managed ConfirmDialog; a rollback error surfaces
 * as an alert. Responsive: cards stack; the diff wraps.
 */
import { useState } from 'react';
import { ApiError } from '@/api';
import type { Assistant, AssistantVersion, ChatModelInfo, Member } from '@/api';
import { Card, CardBody, CardHeader, CardTitle } from '@/components/Card';
import { ConfirmDialog } from '@/components/ConfirmDialog';
import { StatusBadge } from '@/components/StatusBadge';
import { Icon } from '@/ui';
import { useAssistantVersions, useModels, useRollbackAssistant } from '../model/queries';
import { formatDateTime, ownerLabel } from '../model/presentation';
import { configDiff, configSummary } from '../model/versionSummary';

interface VersionHistoryProps {
  /** The assistant whose history to show. Its `version` marks the current head. */
  assistant: Assistant;
  /** The tenant roster for author labels (may be undefined for a non-admin caller). */
  members?: Member[];
}

export function VersionHistory({ assistant, members }: VersionHistoryProps) {
  const versions = useAssistantVersions(assistant.id);
  const models = useModels();
  const modelItems = models.data?.items;

  const pages = versions.data?.pages ?? [];
  const items = pages.flatMap((p) => p.items);
  // The published head config to diff historical versions against. The current
  // head version is the one whose `version` matches the assistant's pointer.
  const head = items.find((v) => v.version === assistant.version) ?? items[0];

  return (
    <section aria-labelledby="version-history-heading" className="space-y-3">
      <div className="flex items-center justify-between gap-2">
        <h2 id="version-history-heading" className="text-sm font-medium">
          Version history
        </h2>
        {assistant.version != null ? (
          <span className="text-xs text-foreground-muted">
            Current: v{assistant.version}
          </span>
        ) : null}
      </div>

      <p className="text-xs text-foreground-muted">
        Every publish freezes an immutable version. Rolling back creates a new
        version equal to the chosen one — history is never changed.
      </p>

      {versions.isPending ? (
        <HistorySkeleton />
      ) : versions.isError ? (
        <HistoryError error={versions.error} onRetry={() => void versions.refetch()} />
      ) : items.length === 0 ? (
        <HistoryEmpty />
      ) : (
        <ol className="space-y-3">
          {items.map((version) => (
            <li key={version.id}>
              <VersionCard
                assistantId={assistant.id}
                version={version}
                head={head}
                isCurrent={version.version === assistant.version}
                models={modelItems}
                members={members}
              />
            </li>
          ))}
        </ol>
      )}

      {versions.hasNextPage ? (
        <div className="flex justify-center pt-1">
          <button
            type="button"
            onClick={() => void versions.fetchNextPage()}
            disabled={versions.isFetchingNextPage}
            className="rounded-md border border-border px-3 py-1.5 text-sm hover:bg-surface-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-60"
          >
            {versions.isFetchingNextPage ? 'Loading…' : 'Load older versions'}
          </button>
        </div>
      ) : null}
    </section>
  );
}

function VersionCard({
  assistantId,
  version,
  head,
  isCurrent,
  models,
  members,
}: {
  assistantId: string;
  version: AssistantVersion;
  head: AssistantVersion | undefined;
  isCurrent: boolean;
  models: ChatModelInfo[] | undefined;
  members: Member[] | undefined;
}) {
  const [confirming, setConfirming] = useState(false);
  const [rollbackError, setRollbackError] = useState<string | null>(null);
  const rollback = useRollbackAssistant(assistantId);

  const summary = configSummary(version.config, models);
  // The diff vs. the current head — only meaningful for a non-current version
  // when a head is known. Empty ⇒ this version's config equals the head.
  const diff = head && !isCurrent ? configDiff(version.config, head.config, models) : [];

  const handleRollback = () => {
    setRollbackError(null);
    rollback.mutate(
      { version: version.version },
      {
        onSuccess: () => setConfirming(false),
        onError: (error) => {
          setRollbackError(
            error instanceof ApiError
              ? error.displayMessage || 'Could not roll back to this version.'
              : 'Could not roll back to this version.',
          );
        },
      },
    );
  };

  return (
    <Card aria-current={isCurrent ? 'true' : undefined}>
      <CardHeader className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <CardTitle>Version {version.version}</CardTitle>
          {isCurrent ? (
            <StatusBadge tone="ok">Current</StatusBadge>
          ) : null}
        </div>
        <div className="flex flex-wrap items-center gap-x-3 gap-y-0.5 text-xs text-foreground-muted">
          <span>{ownerLabel(version.author, members)}</span>
          <time dateTime={version.created_at}>{formatDateTime(version.created_at)}</time>
        </div>
      </CardHeader>

      <CardBody className="space-y-3">
        {version.notes ? (
          <p className="text-sm">{version.notes}</p>
        ) : (
          <p className="text-xs italic text-foreground-muted">No release note.</p>
        )}

        {version.diff_summary ? (
          <p className="rounded-md bg-surface-muted px-2.5 py-1.5 text-xs text-foreground-muted">
            <span className="font-medium">Change from previous:</span> {version.diff_summary}
          </p>
        ) : null}

        {/* Per-version config summary — what this version contains. */}
        <dl className="grid grid-cols-1 gap-x-4 gap-y-1.5 sm:grid-cols-[max-content_1fr]">
          {summary.map((row) => (
            <div key={row.key} className="contents">
              <dt className="text-xs font-medium text-foreground-muted sm:pt-px">{row.label}</dt>
              <dd className="min-w-0 break-words text-sm">{row.value}</dd>
            </div>
          ))}
        </dl>

        {/* Field-level diff vs. the current head (non-current versions only). */}
        {!isCurrent ? (
          <div className="rounded-md border border-border bg-surface-muted/50 p-2.5">
            {diff.length > 0 ? (
              <>
                <p className="mb-1.5 text-xs font-medium text-foreground-muted">
                  Differs from current (v{head?.version}):
                </p>
                <ul className="space-y-1.5">
                  {diff.map((d) => (
                    <li key={d.key} className="text-xs">
                      <span className="font-medium">{d.label}</span>
                      <div className="mt-0.5 grid grid-cols-1 gap-1 sm:grid-cols-2">
                        <span className="min-w-0 break-words rounded bg-danger/10 px-1.5 py-0.5 text-danger">
                          this: {d.from}
                        </span>
                        <span className="min-w-0 break-words rounded bg-ok/10 px-1.5 py-0.5 text-ok">
                          current: {d.to}
                        </span>
                      </div>
                    </li>
                  ))}
                </ul>
              </>
            ) : (
              <p className="text-xs text-foreground-muted">
                Identical config to the current version.
              </p>
            )}
          </div>
        ) : null}

        {/* Rollback action (historical versions only). */}
        {!isCurrent ? (
          <div className="flex items-center gap-2 pt-0.5">
            <button
              type="button"
              onClick={() => {
                setRollbackError(null);
                setConfirming(true);
              }}
              className="inline-flex items-center gap-1.5 rounded-md border border-border px-2.5 py-1 text-xs font-medium hover:bg-surface-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
            >
              <Icon name="corner-down-left" className="shrink-0" />
              Roll back to v{version.version}
            </button>
          </div>
        ) : null}

        {rollbackError ? (
          <p
            role="alert"
            className="flex items-start gap-1.5 rounded-md border border-danger/40 bg-danger/10 p-2 text-xs text-danger"
          >
            <Icon name="alert-triangle" className="mt-px shrink-0" />
            <span>{rollbackError}</span>
          </p>
        ) : null}
      </CardBody>

      <ConfirmDialog
        open={confirming}
        title={`Roll back to version ${version.version}?`}
        description={`This creates a NEW version copying version ${version.version}'s configuration and makes it the current head. Your existing version history is preserved — nothing is overwritten or deleted.`}
        confirmLabel={`Roll back to v${version.version}`}
        busy={rollback.isPending}
        busyLabel="Rolling back…"
        onConfirm={handleRollback}
        onCancel={() => setConfirming(false)}
      />
    </Card>
  );
}

function HistorySkeleton() {
  return (
    <div role="status" aria-busy="true" aria-live="polite" className="space-y-3">
      <span className="sr-only">Loading version history…</span>
      {[0, 1].map((i) => (
        <div key={i} className="space-y-2 rounded-lg border border-border p-4" aria-hidden="true">
          <div className="lc-skeleton" style={{ width: '30%' }} />
          <div className="lc-skeleton" style={{ width: '100%', height: 48 }} />
        </div>
      ))}
    </div>
  );
}

function HistoryEmpty() {
  return (
    <div className="rounded-lg border border-dashed border-border bg-surface-muted/40 p-6 text-center">
      <Icon name="clock" aria-hidden="true" className="mx-auto text-foreground-muted" />
      <p className="mt-2 text-sm font-medium">No versions yet</p>
      <p className="mt-1 text-xs text-foreground-muted">
        Publish this assistant to freeze its first immutable version. Every publish
        adds one to this history.
      </p>
    </div>
  );
}

function HistoryError({ error, onRetry }: { error: unknown; onRetry: () => void }) {
  const status = error instanceof ApiError ? error.status : 0;
  const notFound = status === 404;
  const unauthorized = status === 401;
  const message = notFound
    ? 'This assistant doesn’t exist, or you don’t have access to its history.'
    : unauthorized
      ? 'Your session expired. Sign in again to view version history.'
      : error instanceof ApiError
        ? error.displayMessage || 'Could not load version history.'
        : 'Could not load version history.';

  return (
    <div
      role="alert"
      className="flex flex-col items-center gap-2 rounded-lg border border-danger/40 bg-danger/10 p-6 text-center"
    >
      <Icon name="alert-triangle" aria-hidden="true" />
      <p className="text-sm font-medium text-danger">Couldn’t load version history</p>
      <p className="max-w-sm text-xs text-foreground-muted">{message}</p>
      {!unauthorized && !notFound ? (
        <button
          type="button"
          onClick={onRetry}
          className="rounded-md border border-border bg-surface px-3 py-1.5 text-sm hover:bg-surface-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
        >
          Retry
        </button>
      ) : null}
    </div>
  );
}
