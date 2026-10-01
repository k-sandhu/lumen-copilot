/**
 * Document list per collection (#49 AC-3 · re-laid-out to the documents.html
 * wireframe in #119) — a TABLE whose columns are all backed by REAL document
 * fields: Name + file-type badge, Collection, Visibility (kit PermissionPill,
 * derived from the INV-2 owner-only invariant), Owner (from `owner_id`, "You" for
 * the current user), Updated (kit FreshnessPill, from `updated_at`), Status.
 *
 * Keeps every prior behavior: filter by status + filename (`q`), poll ingestion
 * while unsettled, open the viewer (row click / keyboard), delete (confirm), and
 * a `failed` row's error inline (AC-4). Every state is handled: loading, error
 * (retry), empty vs. filtered-empty.
 */
import { useState } from 'react';
import { ApiError } from '@/api';
import type { Document, DocumentStatus } from '@/api';
import { ConfirmDialog } from '@/components/ConfirmDialog';
import { StatusBadge } from '@/components/StatusBadge';
import { ScrollArea } from '@/components/ScrollArea';
import { FreshnessPill, PermissionPill, StatusDot } from '@/ui';
import { cn } from '@/lib/cn';
import { useDeleteDocument, useDocuments, type DocumentFilters } from '../model/queries';
import {
  documentFreshness,
  fileKind,
  fileKindTone,
  ingestSteps,
  isIngesting,
  ownerLabel,
  statusLabel,
  statusTone,
  visibility,
  type FileKindTone,
} from '../model/presentation';

const STATUS_FILTERS: { value: DocumentStatus | 'all'; label: string }[] = [
  { value: 'all', label: 'All' },
  { value: 'pending', label: 'Queued' },
  { value: 'processing', label: 'Processing' },
  { value: 'ready', label: 'Ready' },
  { value: 'failed', label: 'Failed' },
];

/**
 * Token-driven tint for the file-type badge — only the semantic colors the
 * frontend exposes as Tailwind utilities (no wireframe gradients, no invented
 * `info` color the theme doesn't define).
 */
const KIND_TONE: Record<FileKindTone, string> = {
  pdf: 'bg-danger/15 text-danger',
  doc: 'bg-accent/15 text-accent',
  sheet: 'bg-ok/15 text-ok',
  slide: 'bg-warn/15 text-warn',
  image: 'bg-accent/15 text-accent',
  text: 'bg-foreground-muted/15 text-foreground-muted',
  default: 'bg-surface-muted text-foreground-muted',
};

interface DocumentListProps {
  collectionId: string;
  /** Name of the selected collection (for the Collection column). */
  collectionName?: string;
  /** The signed-in user's id, to label the Owner column ("You"). */
  currentUserId?: string;
  onOpen: (doc: Document) => void;
}

export function DocumentList({
  collectionId,
  collectionName,
  currentUserId,
  onOpen,
}: DocumentListProps) {
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
        <ScrollArea viewportClassName="p-4">
          <ListBody
            query={query}
            filtered={filtered}
            collectionName={collectionName}
            currentUserId={currentUserId}
            onOpen={onOpen}
          />
        </ScrollArea>
      </div>
    </div>
  );
}

type DocsQuery = ReturnType<typeof useDocuments>;

function ListBody({
  query,
  filtered,
  collectionName,
  currentUserId,
  onOpen,
}: {
  query: DocsQuery;
  filtered: boolean;
  collectionName?: string;
  currentUserId?: string;
  onOpen: (doc: Document) => void;
}) {
  if (query.isPending) {
    return (
      <div role="status" aria-live="polite" aria-busy="true" className="space-y-2">
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
      <div role="alert" className="space-y-2 py-2 text-sm">
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
      <p className="py-10 text-center text-sm text-foreground-muted">
        {filtered
          ? 'No documents match these filters.'
          : 'This collection is empty. Upload files above to get started.'}
      </p>
    );
  }

  return (
    <div className="overflow-x-auto rounded-lg border border-border">
      <table className="w-full border-collapse text-left text-sm">
        <caption className="sr-only">Documents in this collection</caption>
        <thead>
          <tr className="border-b border-border bg-surface-muted/50 text-xs uppercase tracking-wide text-foreground-muted">
            <th scope="col" className="px-3 py-2 font-medium">
              Name
            </th>
            <th scope="col" className="px-3 py-2 font-medium">
              Collection
            </th>
            <th scope="col" className="px-3 py-2 font-medium">
              Visibility
            </th>
            <th scope="col" className="px-3 py-2 font-medium">
              Owner
            </th>
            <th scope="col" className="px-3 py-2 font-medium">
              Updated
            </th>
            <th scope="col" className="px-3 py-2 font-medium">
              Status
            </th>
            <th scope="col" className="px-3 py-2 text-right font-medium">
              <span className="sr-only">Actions</span>
            </th>
          </tr>
        </thead>
        <tbody aria-label="Documents">
          {docs.map((doc) => (
            <DocumentRow
              key={doc.id}
              doc={doc}
              collectionName={collectionName}
              currentUserId={currentUserId}
              onOpen={() => onOpen(doc)}
            />
          ))}
        </tbody>
      </table>
    </div>
  );
}

/**
 * The active ingest-stage label for an in-progress document (#89), e.g.
 * "Parsing…" / "Chunking…" / "Embedding…" — derived from the parse→chunk→embed
 * pipeline so the row reflects where ingestion actually is.
 */
function ingestStageLabel(doc: Document): string {
  const active = ingestSteps(doc).find((s) => s.state === 'active' || s.state === 'pending');
  switch (active?.key) {
    case 'parse':
      return 'Parsing…';
    case 'chunk':
      return 'Chunking…';
    case 'embed':
      return 'Embedding…';
    default:
      return 'Processing…';
  }
}

function StatusCell({ doc }: { doc: Document }) {
  if (isIngesting(doc.status)) {
    return (
      <StatusDot
        tone={doc.status === 'processing' ? 'sync' : 'muted'}
        label={ingestStageLabel(doc)}
      />
    );
  }
  return (
    <StatusBadge tone={statusTone(doc.status)} detail={doc.error ?? undefined}>
      {statusLabel(doc.status)}
    </StatusBadge>
  );
}

function DocumentRow({
  doc,
  collectionName,
  currentUserId,
  onOpen,
}: {
  doc: Document;
  collectionName?: string;
  currentUserId?: string;
  onOpen: () => void;
}) {
  const remove = useDeleteDocument();
  // Confirm-before-destructive via the shared focus-managed dialog (mirrors
  // SourcesPanel's pending-target state machine), replacing native window.confirm.
  const [confirmOpen, setConfirmOpen] = useState(false);
  const ready = doc.status === 'ready';
  const kind = fileKind(doc);
  const fresh = documentFreshness(doc);
  const vis = visibility(doc, currentUserId);

  return (
    <>
      <tr
        className={cn(
          'border-b border-border/60 transition-colors last:border-0',
          ready ? 'cursor-pointer hover:bg-surface-muted/60' : 'opacity-90',
        )}
        // Whole-row opens the viewer (the wireframe's `data-drawer-open`), but only
        // once the document is ready (no bytes to preview before then). Keyboard
        // users get the same affordance.
        onClick={ready ? onOpen : undefined}
        onKeyDown={
          ready
            ? (e) => {
                if (e.key === 'Enter' || e.key === ' ') {
                  e.preventDefault();
                  onOpen();
                }
              }
            : undefined
        }
        tabIndex={ready ? 0 : undefined}
        role={ready ? 'button' : undefined}
        aria-label={ready ? `Open ${doc.filename}` : undefined}
      >
        <td className="px-3 py-2.5 align-middle">
          <div className="flex min-w-0 items-center gap-2.5">
            <span
              aria-hidden="true"
              className={cn(
                'grid h-7 w-9 shrink-0 place-items-center rounded text-[10px] font-bold',
                KIND_TONE[fileKindTone(doc)],
              )}
            >
              {kind.slice(0, 4)}
            </span>
            <div className="min-w-0">
              <div className="truncate font-medium text-foreground" title={doc.filename}>
                {doc.filename}
              </div>
              <div className="text-[11px] text-foreground-muted">
                {kind}
                {ready && doc.chunk_count > 0 && <span> · {doc.chunk_count} chunks</span>}
              </div>
            </div>
          </div>
        </td>
        <td className="px-3 py-2.5 align-middle text-foreground-muted">{collectionName ?? '—'}</td>
        <td className="px-3 py-2.5 align-middle">
          <PermissionPill level={vis.level} label={vis.label} title={vis.title} />
        </td>
        <td className="px-3 py-2.5 align-middle text-foreground-muted">
          {ownerLabel(doc, currentUserId)}
        </td>
        <td className="px-3 py-2.5 align-middle">
          {fresh ? (
            <FreshnessPill label={fresh.label} stale={fresh.stale} title={fresh.title} />
          ) : (
            <span className="text-foreground-muted">—</span>
          )}
        </td>
        <td className="px-3 py-2.5 align-middle">
          <StatusCell doc={doc} />
        </td>
        <td className="px-3 py-2.5 text-right align-middle">
          <button
            type="button"
            disabled={remove.isPending}
            onClick={(e) => {
              e.stopPropagation();
              setConfirmOpen(true);
            }}
            // The row is itself a button (Enter/Space opens the viewer). The
            // Delete button's own Enter/Space already fires its click, so we must
            // also stop the keydown bubbling to the row — otherwise the row's
            // onKeyDown would open the viewer alongside the delete.
            onKeyDown={(e) => {
              if (e.key === 'Enter' || e.key === ' ') {
                e.stopPropagation();
              }
            }}
            aria-label={`Delete ${doc.filename}`}
            className="rounded-md px-2.5 py-1 text-xs text-danger hover:bg-surface-muted disabled:opacity-60"
          >
            Delete
          </button>
          {/* `fixed`-positioned overlay; a <div> here is valid inside the cell and
              renders nothing until opened. */}
          <ConfirmDialog
            open={confirmOpen}
            title="Delete document?"
            description={`Delete “${doc.filename}”? This permanently removes it and its indexed chunks. This can't be undone.`}
            confirmLabel="Delete"
            busyLabel="Deleting…"
            busy={remove.isPending}
            onConfirm={() => {
              remove.mutate(doc.id);
              setConfirmOpen(false);
            }}
            onCancel={() => setConfirmOpen(false)}
          />
        </td>
      </tr>

      {(doc.status === 'failed' || remove.isError) && (
        <tr>
          <td colSpan={7} className="px-3 pb-2">
            {doc.status === 'failed' && (
              <p role="alert" className="rounded bg-danger/10 px-2 py-1 text-xs text-danger">
                {doc.error ?? 'Ingestion failed for this document.'}
              </p>
            )}
            {remove.isError && (
              <p role="alert" className="mt-1 text-xs text-danger">
                {remove.error instanceof ApiError ? remove.error.displayMessage : 'Delete failed.'}
              </p>
            )}
          </td>
        </tr>
      )}
    </>
  );
}
