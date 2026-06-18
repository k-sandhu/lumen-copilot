/**
 * Document list per collection (#49 AC-3) — filter by status and filename (`q`),
 * watch ingestion status (pending→processing→ready→failed, polled while
 * unsettled), open the viewer, delete. Every state is handled: loading, error
 * (retry), empty (vs. empty-because-filtered), and a `failed` row shows its
 * error inline (AC-4).
 */
import { useState } from 'react';
import { ApiError } from '@/api';
import type { Document, DocumentStatus } from '@/api';
import { StatusBadge } from '@/components/StatusBadge';
import { ScrollArea } from '@/components/ScrollArea';
import { useDeleteDocument, useDocuments, type DocumentFilters } from '../model/queries';
import { formatBytes, isIngesting, statusLabel, statusTone } from '../model/presentation';

const STATUS_FILTERS: { value: DocumentStatus | 'all'; label: string }[] = [
  { value: 'all', label: 'All' },
  { value: 'pending', label: 'Queued' },
  { value: 'processing', label: 'Processing' },
  { value: 'ready', label: 'Ready' },
  { value: 'failed', label: 'Failed' },
];

interface DocumentListProps {
  collectionId: string;
  onOpen: (doc: Document) => void;
}

export function DocumentList({ collectionId, onOpen }: DocumentListProps) {
  const [status, setStatus] = useState<DocumentStatus | 'all'>('all');
  const [q, setQ] = useState('');

  const filters: DocumentFilters = {
    collectionId,
    status: status === 'all' ? undefined : status,
    q: q.trim() || undefined,
  };
  const query = useDocuments(filters);
  const filtered = status !== 'all' || q.trim().length > 0;

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="shrink-0 space-y-2 border-b border-border px-4 py-3">
        <div className="flex items-center gap-2">
          <input
            type="search"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="Filter by filename…"
            aria-label="Filter documents by filename"
            className="min-w-0 flex-1 rounded-md border border-border bg-surface px-3 py-1.5 text-sm"
          />
          {query.isFetching && (
            <span className="text-xs text-foreground-muted" aria-live="polite">
              Refreshing…
            </span>
          )}
        </div>
        <div className="flex flex-wrap gap-1" role="group" aria-label="Filter by status">
          {STATUS_FILTERS.map((f) => (
            <button
              key={f.value}
              type="button"
              onClick={() => setStatus(f.value)}
              aria-pressed={status === f.value}
              className={
                status === f.value
                  ? 'rounded-full bg-accent/15 px-2.5 py-0.5 text-xs font-medium text-accent'
                  : 'rounded-full border border-border px-2.5 py-0.5 text-xs hover:bg-surface-muted'
              }
            >
              {f.label}
            </button>
          ))}
        </div>
      </div>

      <div className="min-h-0 flex-1">
        <ScrollArea viewportClassName="px-2 py-2">
          <ListBody query={query} filtered={filtered} onOpen={onOpen} />
        </ScrollArea>
      </div>
    </div>
  );
}

type DocsQuery = ReturnType<typeof useDocuments>;

function ListBody({
  query,
  filtered,
  onOpen,
}: {
  query: DocsQuery;
  filtered: boolean;
  onOpen: (doc: Document) => void;
}) {
  if (query.isPending) {
    return (
      <div role="status" aria-live="polite" aria-busy="true" className="space-y-2 px-1">
        <span className="sr-only">Loading documents…</span>
        {[0, 1, 2, 3].map((i) => (
          <div key={i} className="h-12 animate-pulse rounded bg-surface-muted" aria-hidden="true" />
        ))}
      </div>
    );
  }

  if (query.isError) {
    const message =
      query.error instanceof ApiError ? query.error.displayMessage : 'Could not load documents.';
    return (
      <div role="alert" className="space-y-2 px-1 py-2 text-sm">
        <p className="font-medium text-danger">Couldn’t load documents</p>
        <p className="text-foreground-muted">{message}</p>
        <button
          type="button"
          onClick={() => void query.refetch()}
          disabled={query.isFetching}
          className="rounded-md border border-border bg-surface px-3 py-1.5 hover:bg-surface-muted disabled:opacity-60"
        >
          {query.isFetching ? 'Retrying…' : 'Retry'}
        </button>
      </div>
    );
  }

  const docs = query.data.items;
  if (docs.length === 0) {
    return (
      <p className="px-2 py-10 text-center text-sm text-foreground-muted">
        {filtered
          ? 'No documents match these filters.'
          : 'This collection is empty. Upload files above to get started.'}
      </p>
    );
  }

  return (
    <ul aria-label="Documents" className="space-y-1">
      {docs.map((doc) => (
        <DocumentRow key={doc.id} doc={doc} onOpen={() => onOpen(doc)} />
      ))}
    </ul>
  );
}

function DocumentRow({ doc, onOpen }: { doc: Document; onOpen: () => void }) {
  const remove = useDeleteDocument();
  const ingesting = isIngesting(doc.status);

  return (
    <li className="rounded-md border border-border px-3 py-2">
      <div className="flex items-center gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="truncate text-sm font-medium">{doc.filename}</span>
            <StatusBadge tone={statusTone(doc.status)} pulse={ingesting} detail={doc.error}>
              {statusLabel(doc.status)}
            </StatusBadge>
          </div>
          <p className="mt-0.5 text-xs text-foreground-muted">
            {formatBytes(doc.size_bytes)}
            {doc.status === 'ready' && doc.chunk_count > 0 && (
              <span> · {doc.chunk_count} chunks</span>
            )}
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-1">
          <button
            type="button"
            onClick={onOpen}
            disabled={doc.status !== 'ready'}
            title={doc.status === 'ready' ? 'Open document' : 'Available once ingestion completes'}
            className="rounded-md border border-border px-2.5 py-1 text-xs hover:bg-surface-muted disabled:opacity-50"
          >
            Open
          </button>
          <button
            type="button"
            disabled={remove.isPending}
            onClick={() => {
              if (
                typeof window !== 'undefined' &&
                !window.confirm(`Delete “${doc.filename}”?`)
              ) {
                return;
              }
              remove.mutate(doc.id);
            }}
            aria-label={`Delete ${doc.filename}`}
            className="rounded-md px-2.5 py-1 text-xs text-danger hover:bg-surface-muted disabled:opacity-60"
          >
            Delete
          </button>
        </div>
      </div>

      {doc.status === 'failed' && (
        <p role="alert" className="mt-1.5 rounded bg-danger/10 px-2 py-1 text-xs text-danger">
          {doc.error ?? 'Ingestion failed for this document.'}
        </p>
      )}
      {remove.isError && (
        <p role="alert" className="mt-1.5 text-xs text-danger">
          {remove.error instanceof ApiError ? remove.error.displayMessage : 'Delete failed.'}
        </p>
      )}
    </li>
  );
}
