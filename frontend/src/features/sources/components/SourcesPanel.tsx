/**
 * SourcesPanel (#27, ADR-0009; #455, ADR-0019) — the Sources screen body: the
 * connector grid plus the add / re-sync / remove flows, to the wireframe
 * (docs/wireframes/sources.html), extended for managed connectors: the Google
 * Drive add flow (config → `connect` → consent redirect), the OAuth
 * return-state banner (`?connect=ok|error`, the frozen callback contract), the
 * per-source health surface, and the Connect / Reauthorize actions.
 *
 * Implements EVERY async state (frontend/AGENTS.md "every state, not just
 * success"):
 *   loading → a KPI + card skeleton grid
 *   error   → an actionable message with retry (401 messaged distinctly, INV-4)
 *   empty   → "Add your first source — paste a link" with a primary CTA
 *   success → KPI summary + the connector grid + the dashed "add" tile
 *
 * ADMIN GATING (INV-5): managed-connector affordances (gdrive add / connect /
 * reauthorize) exist ONLY for tenant admins — derived from the /auth/me roles.
 * A direct 403 (e.g. a role revoked server-side mid-session) surfaces as an
 * inline error on the card, never a blank pane (the #160 lesson).
 *
 * Mutations live in the model hooks; this composes them and tracks which source
 * a per-source action targets so only that card shows its in-flight state. The
 * grid scrolls independently inside a min-h-0 column so many sources / long
 * URLs never force a whole-page scroll.
 */
import { useCallback, useMemo, useState } from 'react';
import { ApiError } from '@/api';
import { ScrollArea } from '@/components/ScrollArea';
import { Icon, KpiCard } from '@/ui';
import { useCurrentUser } from '@/features/auth';
import { useConnectSource, useDeleteSource, useSources, useSyncSource } from '../model/queries';
import {
  connectReturnErrorMessage,
  connectSourceErrorMessage,
  deleteSourceErrorMessage,
  isGdriveSource,
  relativeTime,
  sourceDetail,
  sourceName,
} from '../model/presentation';
import { navigateToConsent } from '../model/browser';
import { useConnectReturn } from '../model/useConnectReturn';
import type { Source } from '../model/types';
import { SourceCard } from './SourceCard';
import { AddSourceModal } from './AddSourceModal';
import { ConfirmDialog } from '@/components/ConfirmDialog';

export function SourcesPanel() {
  const query = useSources();
  const sync = useSyncSource();
  const connect = useConnectSource();
  const remove = useDeleteSource();
  const currentUser = useCurrentUser();
  const connectReturn = useConnectReturn();

  // Managed-connector affordances are tenant-admin only (ADR-0019 §1, INV-5).
  const isAdmin =
    Array.isArray(currentUser.data?.roles) && currentUser.data.roles.includes('admin');

  const [addOpen, setAddOpen] = useState(false);
  // Stable so the modal's focus trap doesn't re-fire (and steal focus from a
  // field mid-typing) every time this panel re-renders, e.g. on the sync poll.
  const closeAdd = useCallback(() => setAddOpen(false), []);
  // The source pending removal confirmation (null = dialog closed).
  const [pendingRemove, setPendingRemove] = useState<Source | null>(null);
  // The mapped failure of the LAST confirmed remove — rendered inside the
  // confirm dialog (a 403 from the action-time admin gate must be visible,
  // never silently discarded).
  const [removeError, setRemoveError] = useState<string | null>(null);
  // Track the id each mutation targets so only that card shows its busy state.
  const [syncingId, setSyncingId] = useState<string | null>(null);
  const [syncError, setSyncError] = useState<{ id: string; error: ApiError } | null>(null);
  const [connectingId, setConnectingId] = useState<string | null>(null);
  const [connectError, setConnectError] = useState<{ id: string; error: ApiError } | null>(null);

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

  // Start (or restart) the consent flow for a managed source (Connect /
  // Reauthorize). On success the BROWSER leaves for the provider's consent
  // screen; it returns via the callback's 302 with the connect query params.
  const handleConnect = (source: Source) => {
    setConnectingId(source.id);
    setConnectError(null);
    connect.mutate(source.id, {
      onSuccess: (res) => navigateToConsent(res.authorization_url),
      onError: (error) => {
        const apiError =
          error instanceof ApiError ? error : new ApiError('Could not start the consent flow', 0);
        // Surface the mapped human message on the card (403 → the admin gate,
        // INV-5) — an error state, never a blank pane. The problem body is
        // deliberately dropped so displayMessage renders OUR mapping.
        setConnectError({
          id: source.id,
          error: new ApiError(connectSourceErrorMessage(apiError), apiError.status),
        });
      },
      onSettled: () => setConnectingId(null),
    });
  };

  // Close the confirm on SUCCESS only. On error the dialog stays open with the
  // mapped message (403 = the admin gate re-checked at action time, INV-5) and
  // Cancel remains available — the pane never wedges or goes blank.
  const confirmRemove = () => {
    if (!pendingRemove) return;
    const id = pendingRemove.id;
    setRemoveError(null);
    remove.mutate(id, {
      onSuccess: () => {
        setRemoveError(null);
        setPendingRemove(null);
      },
      onError: (error) =>
        setRemoveError(
          deleteSourceErrorMessage(
            error instanceof ApiError ? error : new ApiError('Remove failed', 0),
          ),
        ),
    });
  };

  const requestRemove = (source: Source) => {
    setRemoveError(null);
    setPendingRemove(source);
  };

  const cancelRemove = () => {
    setRemoveError(null);
    setPendingRemove(null);
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

      {/* OAuth consent return banner (the frozen ?connect=ok|error contract) */}
      {connectReturn.result ? (
        <ConnectReturnBanner result={connectReturn.result} onDismiss={connectReturn.dismiss} />
      ) : null}

      <div className="min-h-0 flex-1">
        <ScrollArea viewportClassName="px-5 py-5">
          <Body
            query={query}
            items={items}
            isAdmin={isAdmin}
            syncingId={syncingId}
            syncError={syncError}
            connectingId={connectingId}
            connectError={connectError}
            removingId={remove.isPending ? (pendingRemove?.id ?? null) : null}
            onAdd={() => setAddOpen(true)}
            onSync={handleSync}
            onConnect={handleConnect}
            onRemove={requestRemove}
          />
        </ScrollArea>
      </div>

      <AddSourceModal open={addOpen} onClose={closeAdd} isAdmin={isAdmin} />
      <ConfirmDialog
        open={pendingRemove !== null}
        title="Remove this source?"
        description={
          pendingRemove
            ? `Removing ${sourceName(pendingRemove)} (${sourceDetail(pendingRemove)}) deletes the ${pendingRemove.indexed_count.toLocaleString()} document${pendingRemove.indexed_count === 1 ? '' : 's'} it ingested. This can't be undone.`
            : ''
        }
        confirmLabel="Remove source"
        busy={remove.isPending}
        error={removeError}
        onConfirm={confirmRemove}
        onCancel={cancelRemove}
      />
    </div>
  );
}

/** The success / error banner for an OAuth consent round-trip return. */
function ConnectReturnBanner({
  result,
  onDismiss,
}: {
  result: NonNullable<ReturnType<typeof useConnectReturn>['result']>;
  onDismiss: () => void;
}) {
  const ok = result.kind === 'ok';
  return (
    <div
      role={ok ? 'status' : 'alert'}
      className={
        ok
          ? 'flex shrink-0 items-start gap-2 border-b border-ok/40 bg-ok/10 px-5 py-2.5 text-sm text-foreground'
          : 'flex shrink-0 items-start gap-2 border-b border-danger/40 bg-danger/10 px-5 py-2.5 text-sm text-foreground'
      }
    >
      <Icon name={ok ? 'check' : 'alert-triangle'} className="mt-0.5 shrink-0" />
      <p className="min-w-0 grow">
        {ok
          ? 'Google Drive connected — the first sync has started.'
          : connectReturnErrorMessage(result.kind === 'error' ? result.reason : null)}
      </p>
      <button
        type="button"
        onClick={onDismiss}
        aria-label="Dismiss"
        className="shrink-0 rounded-md border border-border p-1 text-foreground-muted hover:bg-surface-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
      >
        <Icon name="x" />
      </button>
    </div>
  );
}

type SourcesQuery = ReturnType<typeof useSources>;

function Body({
  query,
  items,
  isAdmin,
  syncingId,
  syncError,
  connectingId,
  connectError,
  removingId,
  onAdd,
  onSync,
  onConnect,
  onRemove,
}: {
  query: SourcesQuery;
  items: Source[];
  isAdmin: boolean;
  syncingId: string | null;
  syncError: { id: string; error: ApiError } | null;
  connectingId: string | null;
  connectError: { id: string; error: ApiError } | null;
  removingId: string | null;
  onAdd: () => void;
  onSync: (source: Source) => void;
  onConnect: (source: Source) => void;
  onRemove: (source: Source) => void;
}) {
  if (query.isPending) return <LoadingGrid />;
  if (query.isError) {
    return (
      <ErrorState
        error={query.error}
        onRetry={() => void query.refetch()}
        busy={query.isFetching}
      />
    );
  }
  if (items.length === 0) return <EmptyState onAdd={onAdd} isAdmin={isAdmin} />;

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
              isAdmin={isAdmin}
              syncing={syncingId === source.id}
              syncError={syncError?.id === source.id ? syncError.error : null}
              connecting={connectingId === source.id}
              connectError={connectError?.id === source.id ? connectError.error : null}
              removing={removingId === source.id}
              onSync={onSync}
              onConnect={onConnect}
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
  const { connected, indexed, attention, lastSynced } = useMemo(() => summarize(items), [items]);
  return (
    <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
      <KpiCard label="Connected sources" value={connected.toLocaleString()} />
      <KpiCard label="Objects indexed" value={indexed.toLocaleString()} />
      <KpiCard
        label="Needs attention"
        value={attention.toLocaleString()}
        delta={attention === 0 ? 'all healthy' : `${attention} need attention`}
        trend={attention === 0 ? 'flat' : 'down'}
      />
      <KpiCard label="Last sync" value={lastSynced ?? '—'} />
    </div>
  );
}

function summarize(items: Source[]): {
  connected: number;
  indexed: number;
  attention: number;
  lastSynced: string | null;
} {
  let indexed = 0;
  let attention = 0;
  let latest = 0;
  for (const s of items) {
    indexed += s.indexed_count;
    // A failed sync, a dead OAuth grant, or an unconsented managed source all
    // need a human — count them in the attention KPI.
    if (
      s.status === 'error' ||
      s.status === 'pending_auth' ||
      (isGdriveSource(s) && s.reauthorize_required)
    ) {
      attention += 1;
    }
    const ts = s.last_synced_at ? Date.parse(s.last_synced_at) : NaN;
    if (!Number.isNaN(ts) && ts > latest) latest = ts;
  }
  return {
    connected: items.length,
    indexed,
    attention,
    lastSynced: latest > 0 ? relativeTime(new Date(latest).toISOString()) : null,
  };
}

function LoadingGrid() {
  return (
    <div role="status" aria-busy="true" aria-live="polite" className="space-y-5">
      <span className="sr-only">Loading connected sources…</span>
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        {[0, 1, 2, 3].map((i) => (
          <div
            key={i}
            className="rounded-xl border border-border bg-surface p-4"
            aria-hidden="true"
          >
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

function EmptyState({ onAdd, isAdmin }: { onAdd: () => void; isAdmin: boolean }) {
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
          {isAdmin
            ? ' As a tenant admin you can also connect Google Drive — its file permissions are mirrored, never widened.'
            : ''}
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
  const message = unauthorized
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
