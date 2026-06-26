/**
 * SourcesPanel (#27, ADR-0009) — the Sources screen body: the connector grid plus
 * the add / re-sync / remove flows, to the wireframe (docs/wireframes/sources.html).
 *
 * Implements EVERY async state (frontend/AGENTS.md "every state, not just
 * success"):
 *   loading → a KPI + card skeleton grid
 *   error   → an actionable message with retry (401 messaged distinctly, INV-4)
 *   empty   → "Add your first source — paste a link" with a primary CTA
 *   success → KPI summary + the connector grid + the dashed "add" tile
 *
 * Mutations live in the model hooks; this composes them and tracks which source a
 * per-source action targets so only that card shows its in-flight state. The grid
 * scrolls independently inside a min-h-0 column so many sources / long URLs never
 * force a whole-page scroll.
 */
import { useMemo, useState } from 'react';
import { ApiError } from '@/api';
import { ScrollArea } from '@/components/ScrollArea';
import { Icon, KpiCard } from '@/ui';
import { useDeleteSource, useSources, useSyncSource } from '../model/queries';
import { relativeTime } from '../model/presentation';
import type { Source } from '../model/types';
import { SourceCard } from './SourceCard';
import { AddSourceModal } from './AddSourceModal';
import { ConfirmDialog } from './ConfirmDialog';

export function SourcesPanel() {
  const query = useSources();
  const sync = useSyncSource();
  const remove = useDeleteSource();

  const [addOpen, setAddOpen] = useState(false);
  // The source pending removal confirmation (null = dialog closed).
  const [pendingRemove, setPendingRemove] = useState<Source | null>(null);
  // Track the id each mutation targets so only that card shows its busy state.
  const [syncingId, setSyncingId] = useState<string | null>(null);
  const [syncError, setSyncError] = useState<{ id: string; error: ApiError } | null>(null);

  const items = query.data?.items ?? [];

  const handleSync = (source: Source) => {
    setSyncingId(source.id);
    setSyncError(null);
    sync.mutate(source.id, {
      onError: (error) =>
        setSyncError({
          id: source.id,
          error: error instanceof ApiError ? error : new ApiError('Sync failed', 0),
        }),
      onSuccess: () => setSyncError(null),
      onSettled: () => setSyncingId(null),
    });
  };

  const confirmRemove = () => {
    if (!pendingRemove) return;
    const id = pendingRemove.id;
    remove.mutate(id, { onSettled: () => setPendingRemove(null) });
  };

  return (
    <div className="flex h-full min-h-0 flex-col">
      {/* Header */}
      <header className="flex shrink-0 flex-wrap items-start justify-between gap-3 border-b border-border px-5 py-4">
        <div className="min-w-0">
          <h1 className="text-base font-semibold">Sources</h1>
          <p className="mt-0.5 text-sm text-foreground-muted">
            Connected systems Lumen reads from — source permissions are mirrored, never widened.
          </p>
        </div>
        <button
          type="button"
          onClick={() => setAddOpen(true)}
          className="inline-flex shrink-0 items-center gap-1.5 rounded-md bg-accent px-3 py-1.5 text-sm font-medium text-white hover:opacity-90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
        >
          <Icon name="plug" className="shrink-0" />
          Add source
        </button>
      </header>

      <div className="min-h-0 flex-1">
        <ScrollArea viewportClassName="px-5 py-5">
          <Body
            query={query}
            items={items}
            syncingId={syncingId}
            syncError={syncError}
            removingId={remove.isPending ? (pendingRemove?.id ?? null) : null}
            onAdd={() => setAddOpen(true)}
            onSync={handleSync}
            onRemove={setPendingRemove}
          />
        </ScrollArea>
      </div>

      <AddSourceModal open={addOpen} onClose={() => setAddOpen(false)} />
      <ConfirmDialog
        open={pendingRemove !== null}
        title="Remove this source?"
        description={
          pendingRemove
            ? `Removing ${pendingRemove.config.url} deletes the ${pendingRemove.indexed_count.toLocaleString()} document${pendingRemove.indexed_count === 1 ? '' : 's'} it ingested. This can't be undone.`
            : ''
        }
        confirmLabel="Remove source"
        busy={remove.isPending}
        onConfirm={confirmRemove}
        onCancel={() => setPendingRemove(null)}
      />
    </div>
  );
}

type SourcesQuery = ReturnType<typeof useSources>;

function Body({
  query,
  items,
  syncingId,
  syncError,
  removingId,
  onAdd,
  onSync,
  onRemove,
}: {
  query: SourcesQuery;
  items: Source[];
  syncingId: string | null;
  syncError: { id: string; error: ApiError } | null;
  removingId: string | null;
  onAdd: () => void;
  onSync: (source: Source) => void;
  onRemove: (source: Source) => void;
}) {
  if (query.isPending) return <LoadingGrid />;
  if (query.isError) {
    return <ErrorState error={query.error} onRetry={() => void query.refetch()} busy={query.isFetching} />;
  }
  if (items.length === 0) return <EmptyState onAdd={onAdd} />;

  return (
    <div className="space-y-5">
      <KpiRow items={items} />
      <ul
        aria-label="Connected sources"
        className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-3"
      >
        {items.map((source) => (
          <li key={source.id} className="min-w-0">
            <SourceCard
              source={source}
              syncing={syncingId === source.id}
              syncError={syncError?.id === source.id ? syncError.error : null}
              removing={removingId === source.id}
              onSync={onSync}
              onRemove={onRemove}
            />
          </li>
        ))}
        <li className="min-w-0">
          <AddTile onAdd={onAdd} />
        </li>
      </ul>
    </div>
  );
}

function KpiRow({ items }: { items: Source[] }) {
  const { connected, indexed, errors, lastSynced } = useMemo(() => summarize(items), [items]);
  return (
    <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
      <KpiCard label="Connected sources" value={connected.toLocaleString()} />
      <KpiCard label="Objects indexed" value={indexed.toLocaleString()} />
      <KpiCard
        label="Needs attention"
        value={errors.toLocaleString()}
        delta={errors === 0 ? 'all healthy' : `${errors} with errors`}
        trend={errors === 0 ? 'flat' : 'down'}
      />
      <KpiCard label="Last sync" value={lastSynced ?? '—'} />
    </div>
  );
}

function summarize(items: Source[]): {
  connected: number;
  indexed: number;
  errors: number;
  lastSynced: string | null;
} {
  let indexed = 0;
  let errors = 0;
  let latest = 0;
  for (const s of items) {
    indexed += s.indexed_count;
    if (s.status === 'error') errors += 1;
    const ts = s.last_synced_at ? Date.parse(s.last_synced_at) : NaN;
    if (!Number.isNaN(ts) && ts > latest) latest = ts;
  }
  return {
    connected: items.length,
    indexed,
    errors,
    lastSynced: latest > 0 ? relativeTime(new Date(latest).toISOString()) : null,
  };
}

function LoadingGrid() {
  return (
    <div role="status" aria-busy="true" aria-live="polite" className="space-y-5">
      <span className="sr-only">Loading connected sources…</span>
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        {[0, 1, 2, 3].map((i) => (
          <div key={i} className="rounded-xl border border-border bg-surface p-4" aria-hidden="true">
            <div className="lc-skeleton" style={{ width: '60%' }} />
            <div className="lc-skeleton" style={{ width: '40%', marginTop: 12, height: 24 }} />
          </div>
        ))}
      </div>
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-3">
        {[0, 1, 2, 3, 4, 5].map((i) => (
          <div
            key={i}
            className="h-48 rounded-xl border border-border bg-surface p-4"
            aria-hidden="true"
          >
            <div className="flex gap-3">
              <div className="lc-skeleton" style={{ width: 40, height: 40, borderRadius: 8 }} />
              <div className="grow space-y-2">
                <div className="lc-skeleton" style={{ width: '50%' }} />
                <div className="lc-skeleton" style={{ width: '80%' }} />
              </div>
            </div>
            <div className="lc-skeleton" style={{ width: '100%', marginTop: 24, height: 36 }} />
          </div>
        ))}
      </div>
    </div>
  );
}

function EmptyState({ onAdd }: { onAdd: () => void }) {
  return (
    <div className="flex flex-col items-center gap-3 rounded-xl border border-dashed border-border px-6 py-20 text-center">
      <span className="grid h-12 w-12 place-items-center rounded-xl bg-surface-muted text-foreground-muted">
        <Icon name="plug" />
      </span>
      <div>
        <p className="text-sm font-medium">Add your first source — paste a link</p>
        <p className="mx-auto mt-1 max-w-sm text-sm text-foreground-muted">
          Connect a web page, an RSS/Atom feed, or a sitemap and Lumen will ingest it so you can
          chat over it. Zero setup — just a URL.
        </p>
      </div>
      <button
        type="button"
        onClick={onAdd}
        className="inline-flex items-center gap-1.5 rounded-md bg-accent px-3 py-1.5 text-sm font-medium text-white hover:opacity-90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
      >
        <Icon name="plug" className="shrink-0" />
        Add source
      </button>
    </div>
  );
}

function ErrorState({
  error,
  onRetry,
  busy,
}: {
  error: unknown;
  onRetry: () => void;
  busy: boolean;
}) {
  // A 401 (expired/missing token, INV-4) is a re-auth dead-end a retry won't fix;
  // message it honestly. Everything else is transient — offer a retry.
  const status = error instanceof ApiError ? error.status : 0;
  const unauthorized = status === 401;
  const message =
    unauthorized
      ? 'Your session expired. Sign in again to manage your sources.'
      : error instanceof ApiError
        ? error.displayMessage || 'Could not load your sources.'
        : 'Could not load your sources.';

  return (
    <div
      role="alert"
      className="flex flex-col items-center gap-3 rounded-xl border border-danger/40 bg-danger/10 p-8 text-center"
    >
      <Icon name="alert-triangle" aria-hidden="true" />
      <p className="text-sm font-medium text-danger">Couldn’t load sources</p>
      <p className="max-w-sm text-sm text-foreground-muted">{message}</p>
      {!unauthorized ? (
        <button
          type="button"
          onClick={onRetry}
          disabled={busy}
          className="rounded-md border border-border bg-surface px-3 py-1.5 text-sm hover:bg-surface-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-60"
        >
          {busy ? 'Retrying…' : 'Retry'}
        </button>
      ) : null}
    </div>
  );
}

function AddTile({ onAdd }: { onAdd: () => void }) {
  return (
    <button
      type="button"
      onClick={onAdd}
      className="flex h-full min-h-[12rem] w-full flex-col items-center justify-center gap-2 rounded-xl border border-dashed border-border bg-surface text-sm text-foreground-muted hover:border-accent/60 hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
    >
      <Icon name="plug" />
      Add a source
    </button>
  );
}
