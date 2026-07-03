/**
 * ArtifactPanel (#222) — the surface that lists, previews, downloads, and deletes
 * the files an assistant produced (E9-4). List + detail in two independently
 * scrollable panes (frontend/AGENTS.md): pick an artifact on the left, preview it
 * on the right; each artifact has a Download button (bytes through the api/
 * boundary, bearer attached) and a Delete behind a ConfirmDialog.
 *
 * Every async state is implemented (AC-3): a loading skeleton, a labelled empty
 * state, an actionable error with Retry, and the populated list. A previewable
 * type (text/markdown/csv/json/image) previews inline via ArtifactPreview; others
 * show a download-only card (AC-2). A failed download surfaces an inline
 * error + retry (negative, AC-3). Sanitized preview only — never raw HTML.
 *
 * Reusable: an optional `sessionId` scopes the panel to one chat session's
 * artifacts (the in-chat panel), and `initialArtifactId` deep-links a specific
 * file (the code-run inspector chip → /artifacts?artifact=<id>). Server data lives
 * in TanStack Query (queries.ts); only the selection + confirm/download UI state
 * is local.
 */
import { useCallback, useEffect, useMemo, useState } from 'react';
import { ApiError, fetchArtifactContent } from '@/api';
import type { Artifact, ArtifactProducedBy } from '@/api';
import { Card } from '@/components/Card';
import { ConfirmDialog } from '@/components/ConfirmDialog';
import { ScrollArea } from '@/components/ScrollArea';
import { Icon } from '@/ui';
import { useArtifacts, useDeleteArtifact, type ArtifactFilters } from '../model/queries';
import { fileKind, formatBytes, previewKind, producedByLabel, relativeTime } from '../model/presentation';
import { ArtifactPreview } from './ArtifactPreview';

const ORIGIN_FILTERS: { value: ArtifactProducedBy | 'all'; label: string }[] = [
  { value: 'all', label: 'All' },
  { value: 'tool', label: 'Tool' },
  { value: 'run', label: 'Run' },
  { value: 'chat_session', label: 'Chat' },
];

export interface ArtifactPanelProps {
  /** Scope to one chat session's artifacts (the in-chat panel). Omit for all. */
  sessionId?: string;
  /** Deep-link a specific artifact selected on first render (code-run chip). */
  initialArtifactId?: string;
  /** Optional heading id so a container can label the region. */
  headingId?: string;
}

export function ArtifactPanel({ sessionId, initialArtifactId, headingId }: ArtifactPanelProps) {
  const [origin, setOrigin] = useState<ArtifactProducedBy | 'all'>('all');
  const [selectedId, setSelectedId] = useState<string | null>(initialArtifactId ?? null);
  const [pendingDelete, setPendingDelete] = useState<Artifact | null>(null);
  const [downloadError, setDownloadError] = useState<string | null>(null);
  const [downloadingId, setDownloadingId] = useState<string | null>(null);

  const filters: ArtifactFilters = {
    ...(sessionId ? { sessionId } : {}),
    ...(origin !== 'all' ? { producedBy: origin } : {}),
  };
  const query = useArtifacts(filters);
  const deleteMutation = useDeleteArtifact();

  const items = useMemo(() => query.data?.items ?? [], [query.data]);
  // Keep a valid selection: fall back to the first item once the list loads, and
  // drop a selection that no longer exists (e.g. after delete / filter change).
  useEffect(() => {
    if (items.length === 0) {
      if (selectedId !== null) setSelectedId(null);
      return;
    }
    if (selectedId === null || !items.some((a) => a.id === selectedId)) {
      setSelectedId(items[0]?.id ?? null);
    }
  }, [items, selectedId]);

  const selected = items.find((a) => a.id === selectedId) ?? null;

  const download = useCallback(async (artifact: Artifact) => {
    setDownloadError(null);
    setDownloadingId(artifact.id);
    try {
      // Bytes on demand through the api/ boundary (bearer attached; 302→presigned
      // followed). Trigger a same-doc anchor download, then revoke on the next tick.
      const content = await fetchArtifactContent(artifact.id);
      const anchor = document.createElement('a');
      anchor.href = content.url;
      anchor.download = artifact.filename;
      anchor.rel = 'noopener';
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      setTimeout(content.revoke, 0);
    } catch (error: unknown) {
      setDownloadError(
        error instanceof ApiError
          ? error.status === 404
            ? 'This artifact is no longer available.'
            : error.displayMessage
          : 'Download failed. Please try again.',
      );
    } finally {
      setDownloadingId(null);
    }
  }, []);

  const confirmDelete = useCallback(() => {
    if (!pendingDelete) return;
    const id = pendingDelete.id;
    deleteMutation.mutate(id, {
      onSuccess: () => {
        setPendingDelete(null);
        if (selectedId === id) setSelectedId(null);
      },
    });
  }, [pendingDelete, deleteMutation, selectedId]);

  return (
    <div className="flex h-full min-h-0 flex-col gap-3">
      <header className="flex flex-wrap items-center justify-between gap-2">
        <h2 id={headingId} className="text-sm font-semibold">
          Artifacts
        </h2>
        {/* Write-origin filter (chat_session / run / tool). */}
        <div className="flex items-center gap-1" role="group" aria-label="Filter by origin">
          {ORIGIN_FILTERS.map((f) => (
            <button
              key={f.value}
              type="button"
              aria-pressed={origin === f.value}
              onClick={() => setOrigin(f.value)}
              className={`rounded-md border px-2 py-1 text-xs focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent ${
                origin === f.value
                  ? 'border-accent bg-accent/10 text-accent'
                  : 'border-border text-foreground-muted hover:bg-surface-muted'
              }`}
            >
              {f.label}
            </button>
          ))}
        </div>
      </header>

      <div className="grid min-h-0 flex-1 grid-cols-1 gap-3 md:grid-cols-[minmax(0,20rem)_1fr]">
        {/* --- List pane (independently scrollable) --- */}
        <Card className="flex min-h-0 flex-col overflow-hidden">
          <ScrollArea className="min-h-0 flex-1" viewportClassName="p-1.5">
            <ArtifactListBody
              query={query}
              items={items}
              selectedId={selectedId}
              onSelect={setSelectedId}
            />
          </ScrollArea>
        </Card>

        {/* --- Detail pane (preview + actions) --- */}
        <Card className="flex min-h-0 flex-col overflow-hidden">
          {selected ? (
            <ArtifactDetail
              artifact={selected}
              downloading={downloadingId === selected.id}
              downloadError={downloadError}
              onDownload={() => void download(selected)}
              onDelete={() => setPendingDelete(selected)}
            />
          ) : (
            <div className="flex h-full items-center justify-center p-6 text-center">
              <p className="text-sm text-foreground-muted">
                {items.length > 0 ? 'Select an artifact to preview it.' : 'No artifact selected.'}
              </p>
            </div>
          )}
        </Card>
      </div>

      <ConfirmDialog
        open={pendingDelete !== null}
        title="Delete artifact?"
        description={
          pendingDelete
            ? `“${pendingDelete.filename}” will be permanently deleted. This can’t be undone.`
            : ''
        }
        confirmLabel="Delete"
        busy={deleteMutation.isPending}
        busyLabel="Deleting…"
        onConfirm={confirmDelete}
        onCancel={() => setPendingDelete(null)}
      />
    </div>
  );
}

/** The list body — every state: loading skeleton, error+retry, empty, rows. */
function ArtifactListBody({
  query,
  items,
  selectedId,
  onSelect,
}: {
  query: ReturnType<typeof useArtifacts>;
  items: Artifact[];
  selectedId: string | null;
  onSelect: (id: string) => void;
}) {
  if (query.isPending) {
    return (
      <div role="status" aria-live="polite" aria-busy="true" className="space-y-1.5 p-1.5">
        <span className="sr-only">Loading artifacts…</span>
        {[0, 1, 2].map((i) => (
          <div key={i} className="h-12 animate-pulse rounded-md bg-surface-muted" aria-hidden="true" />
        ))}
      </div>
    );
  }

  if (query.isError) {
    const status = query.error instanceof ApiError ? query.error.status : 0;
    const message =
      status === 401
        ? 'Your session expired. Sign in again to continue.'
        : query.error instanceof ApiError
          ? query.error.displayMessage || 'Couldn’t load your artifacts.'
          : 'Couldn’t load your artifacts.';
    return (
      <div role="alert" className="space-y-2 p-3 text-sm">
        <p className="flex items-center gap-2 font-medium text-danger">
          <Icon name="alert-triangle" className="shrink-0" />
          Couldn’t load artifacts
        </p>
        <p className="text-foreground-muted">{message}</p>
        {status !== 401 ? (
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

  if (items.length === 0) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-1 p-6 text-center">
        <Icon name="inbox" className="text-foreground-muted" />
        <p className="text-sm font-medium">No artifacts yet</p>
        <p className="text-xs text-foreground-muted">
          Files an assistant produces (written files, code-run output) will appear here.
        </p>
      </div>
    );
  }

  return (
    <ul className="space-y-1" aria-label="Artifacts">
      {items.map((artifact) => {
        const active = artifact.id === selectedId;
        const when = relativeTime(artifact.created_at);
        return (
          <li key={artifact.id}>
            <button
              type="button"
              aria-current={active || undefined}
              onClick={() => onSelect(artifact.id)}
              className={`flex w-full items-center gap-2 rounded-md px-2 py-2 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent ${
                active ? 'bg-accent/10 ring-1 ring-accent/40' : 'hover:bg-surface-muted'
              }`}
            >
              <Icon name="file-text" className="shrink-0 text-foreground-muted" />
              <span className="min-w-0 flex-1">
                <span className="block truncate text-sm font-medium">{artifact.filename}</span>
                <span className="block truncate text-xs text-foreground-muted">
                  {fileKind(artifact)} · {formatBytes(artifact.size_bytes)}
                  {when ? ` · ${when}` : ''}
                </span>
              </span>
            </button>
          </li>
        );
      })}
    </ul>
  );
}

/** The detail pane for the selected artifact — metadata, actions, and a preview. */
function ArtifactDetail({
  artifact,
  downloading,
  downloadError,
  onDownload,
  onDelete,
}: {
  artifact: Artifact;
  downloading: boolean;
  downloadError: string | null;
  onDownload: () => void;
  onDelete: () => void;
}) {
  const canPreview = previewKind(artifact.mime_type) !== 'download';
  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex shrink-0 flex-wrap items-start justify-between gap-2 border-b border-border px-4 py-3">
        <div className="min-w-0">
          <p className="truncate text-sm font-semibold" title={artifact.filename}>
            {artifact.filename}
          </p>
          <p className="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-xs text-foreground-muted">
            <span>{fileKind(artifact)}</span>
            <span aria-hidden="true">·</span>
            <span>{formatBytes(artifact.size_bytes)}</span>
            <span aria-hidden="true">·</span>
            <span>From {producedByLabel(artifact.produced_by)}</span>
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <button
            type="button"
            onClick={onDownload}
            disabled={downloading}
            className="inline-flex items-center gap-1 rounded-md border border-border bg-surface px-2.5 py-1.5 text-xs font-medium hover:bg-surface-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-60"
          >
            <Icon name="arrow-up-right" className="shrink-0" />
            {downloading ? 'Downloading…' : 'Download'}
          </button>
          <button
            type="button"
            onClick={onDelete}
            aria-label={`Delete ${artifact.filename}`}
            className="inline-flex items-center gap-1 rounded-md border border-border bg-surface px-2.5 py-1.5 text-xs font-medium text-danger hover:bg-danger/10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-danger"
          >
            <Icon name="trash" className="shrink-0" />
            Delete
          </button>
        </div>
      </div>

      {downloadError ? (
        <p role="alert" className="shrink-0 border-b border-border bg-danger/10 px-4 py-2 text-xs text-danger">
          {downloadError}
        </p>
      ) : null}

      {/* Preview (independently scrollable). Non-previewable → a download card. */}
      <div className="min-h-0 flex-1">
        {canPreview ? (
          <ArtifactPreview
            key={artifact.id}
            artifactId={artifact.id}
            filename={artifact.filename}
            mimeType={artifact.mime_type}
          />
        ) : (
          <div className="flex h-full flex-col items-center justify-center gap-2 p-6 text-center">
            <Icon name="file-text" className="text-foreground-muted" />
            <p className="text-sm text-foreground-muted">No inline preview for this file type.</p>
            <button
              type="button"
              onClick={onDownload}
              disabled={downloading}
              className="rounded-md border border-border bg-surface px-3 py-1.5 text-sm hover:bg-surface-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-60"
            >
              {downloading ? 'Downloading…' : 'Download original'}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
