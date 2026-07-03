/**
 * ServersPanel (#228, ADR-0012) — the MCP-servers screen body: the server grid
 * plus the register / test / enable-disable / remove flows and the detail drawer.
 * Mirrors the Sources connector-grid UX (SourcesPanel).
 *
 * Implements EVERY async state (frontend/AGENTS.md "every state, not just
 * success"):
 *   loading → a KPI + card skeleton grid
 *   error   → an actionable message with retry (401 messaged distinctly, INV-4)
 *   empty   → "Register your first MCP server" with a primary CTA
 *   success → KPI summary + the server grid + the dashed "register" tile
 *
 * Mutations live in the model hooks; this composes them and tracks which server a
 * per-server action targets so only that card shows its in-flight state. The grid
 * scrolls independently inside a min-h-0 column so many servers / long endpoints
 * never force a whole-page scroll.
 */
import { useMemo, useState } from 'react';
import { ApiError } from '@/api';
import { ScrollArea } from '@/components/ScrollArea';
import { ConfirmDialog } from '@/components/ConfirmDialog';
import { Icon, KpiCard } from '@/ui';
import {
  useDeleteMcpServer,
  useMcpServers,
  useTestMcpServer,
  useUpdateMcpServer,
} from '../model/queries';
import type { McpServer } from '../model/types';
import { ServerCard } from './ServerCard';
import { RegisterServerModal } from './RegisterServerModal';
import { ServerDetailDrawer } from './ServerDetailDrawer';

export function ServersPanel() {
  const query = useMcpServers();
  const remove = useDeleteMcpServer();

  const [registerOpen, setRegisterOpen] = useState(false);
  const [detailId, setDetailId] = useState<string | null>(null);
  // The server pending removal confirmation (null = dialog closed).
  const [pendingRemove, setPendingRemove] = useState<McpServer | null>(null);
  // Track the id each per-server mutation targets so only that card shows busy.
  const [testingId, setTestingId] = useState<string | null>(null);
  const [testError, setTestError] = useState<{ id: string; error: ApiError } | null>(null);
  const [togglingId, setTogglingId] = useState<string | null>(null);

  const items = query.data?.items ?? [];

  const confirmRemove = () => {
    if (!pendingRemove) return;
    const id = pendingRemove.id;
    remove.mutate(id, {
      onSettled: () => {
        setPendingRemove(null);
        if (detailId === id) setDetailId(null);
      },
    });
  };

  return (
    <div className="flex h-full min-h-0 flex-col">
      <header className="flex shrink-0 flex-wrap items-start justify-between gap-3 border-b border-border px-5 py-4">
        <div className="min-w-0">
          <h1 className="text-base font-semibold">MCP servers</h1>
          <p className="mt-0.5 text-sm text-foreground-muted">
            Remote Model Context Protocol servers whose tools your assistants can use — each
            registered, tested, and scoped to you within your tenant.
          </p>
        </div>
        <button
          type="button"
          onClick={() => setRegisterOpen(true)}
          className="inline-flex shrink-0 items-center gap-1.5 rounded-md bg-accent px-3 py-1.5 text-sm font-medium text-white hover:opacity-90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
        >
          <Icon name="plus" className="shrink-0" />
          Register server
        </button>
      </header>

      <div className="min-h-0 flex-1">
        <ScrollArea viewportClassName="px-5 py-5">
          <Body
            query={query}
            items={items}
            testingId={testingId}
            testError={testError}
            togglingId={togglingId}
            removingId={remove.isPending ? (pendingRemove?.id ?? null) : null}
            onRegister={() => setRegisterOpen(true)}
            onOpen={(s) => setDetailId(s.id)}
            onRemove={setPendingRemove}
            onTestStart={(id) => {
              setTestingId(id);
              setTestError(null);
            }}
            onTestSettled={() => setTestingId(null)}
            onTestError={(id, error) => setTestError({ id, error })}
            onToggleStart={setTogglingId}
            onToggleSettled={() => setTogglingId(null)}
          />
        </ScrollArea>
      </div>

      <RegisterServerModal open={registerOpen} onClose={() => setRegisterOpen(false)} />
      <ServerDetailDrawer serverId={detailId} onClose={() => setDetailId(null)} />
      <ConfirmDialog
        open={pendingRemove !== null}
        title="Remove this MCP server?"
        description={
          pendingRemove
            ? `Removing ${pendingRemove.name} deletes its stored credential and its discovered tools — assistants can no longer use them. This can't be undone.`
            : ''
        }
        confirmLabel="Remove server"
        busy={remove.isPending}
        onConfirm={confirmRemove}
        onCancel={() => setPendingRemove(null)}
      />
    </div>
  );
}

type ServersQuery = ReturnType<typeof useMcpServers>;

interface BodyProps {
  query: ServersQuery;
  items: McpServer[];
  testingId: string | null;
  testError: { id: string; error: ApiError } | null;
  togglingId: string | null;
  removingId: string | null;
  onRegister: () => void;
  onOpen: (server: McpServer) => void;
  onRemove: (server: McpServer) => void;
  onTestStart: (id: string) => void;
  onTestSettled: () => void;
  onTestError: (id: string, error: ApiError) => void;
  onToggleStart: (id: string) => void;
  onToggleSettled: () => void;
}

function Body({
  query,
  items,
  testingId,
  testError,
  togglingId,
  removingId,
  onRegister,
  onOpen,
  onRemove,
  onTestStart,
  onTestSettled,
  onTestError,
  onToggleStart,
  onToggleSettled,
}: BodyProps) {
  if (query.isPending) return <LoadingGrid />;
  if (query.isError) {
    return (
      <ErrorState error={query.error} onRetry={() => void query.refetch()} busy={query.isFetching} />
    );
  }
  if (items.length === 0) return <EmptyState onRegister={onRegister} />;

  return (
    <div className="space-y-5">
      <KpiRow items={items} />
      <ul
        aria-label="Registered MCP servers"
        className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-3"
      >
        {items.map((server) => (
          <li key={server.id} className="min-w-0">
            <ServerCardConnected
              server={server}
              testing={testingId === server.id}
              testError={testError?.id === server.id ? testError.error : null}
              toggling={togglingId === server.id}
              removing={removingId === server.id}
              onOpen={onOpen}
              onRemove={onRemove}
              onTestStart={onTestStart}
              onTestSettled={onTestSettled}
              onTestError={onTestError}
              onToggleStart={onToggleStart}
              onToggleSettled={onToggleSettled}
            />
          </li>
        ))}
        <li className="min-w-0">
          <RegisterTile onRegister={onRegister} />
        </li>
      </ul>
    </div>
  );
}

/**
 * Wraps a ServerCard with its per-server test + toggle mutations (each keyed by
 * the server id) so a busy state stays scoped to the one card acting.
 */
function ServerCardConnected({
  server,
  testing,
  testError,
  toggling,
  removing,
  onOpen,
  onRemove,
  onTestStart,
  onTestSettled,
  onTestError,
  onToggleStart,
  onToggleSettled,
}: {
  server: McpServer;
  testing: boolean;
  testError: ApiError | null;
  toggling: boolean;
  removing: boolean;
  onOpen: (server: McpServer) => void;
  onRemove: (server: McpServer) => void;
  onTestStart: (id: string) => void;
  onTestSettled: () => void;
  onTestError: (id: string, error: ApiError) => void;
  onToggleStart: (id: string) => void;
  onToggleSettled: () => void;
}) {
  const test = useTestMcpServer(server.id);
  const update = useUpdateMcpServer(server.id);

  const handleTest = () => {
    onTestStart(server.id);
    test.mutate(undefined, {
      onError: (error) =>
        onTestError(server.id, error instanceof ApiError ? error : new ApiError('Test failed', 0)),
      onSettled: () => onTestSettled(),
    });
  };

  const handleToggle = () => {
    onToggleStart(server.id);
    update.mutate({ enabled: !server.enabled }, { onSettled: () => onToggleSettled() });
  };

  return (
    <ServerCard
      server={server}
      testing={testing}
      testError={testError}
      toggling={toggling}
      removing={removing}
      onTest={handleTest}
      onToggle={handleToggle}
      onOpen={onOpen}
      onRemove={onRemove}
    />
  );
}

function KpiRow({ items }: { items: McpServer[] }) {
  const { registered, tools, errors, enabled } = useMemo(() => summarize(items), [items]);
  return (
    <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
      <KpiCard label="Registered servers" value={registered.toLocaleString()} />
      <KpiCard label="Discovered tools" value={tools.toLocaleString()} />
      <KpiCard label="Enabled" value={enabled.toLocaleString()} />
      <KpiCard
        label="Needs attention"
        value={errors.toLocaleString()}
        delta={errors === 0 ? 'all healthy' : `${errors} unreachable`}
        trend={errors === 0 ? 'flat' : 'down'}
      />
    </div>
  );
}

function summarize(items: McpServer[]): {
  registered: number;
  tools: number;
  errors: number;
  enabled: number;
} {
  let tools = 0;
  let errors = 0;
  let enabled = 0;
  for (const s of items) {
    tools += s.discovered_tool_count;
    if (s.status === 'error') errors += 1;
    if (s.enabled) enabled += 1;
  }
  return { registered: items.length, tools, errors, enabled };
}

function LoadingGrid() {
  return (
    <div role="status" aria-busy="true" aria-live="polite" className="space-y-5">
      <span className="sr-only">Loading MCP servers…</span>
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
            className="h-52 rounded-xl border border-border bg-surface p-4"
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

function EmptyState({ onRegister }: { onRegister: () => void }) {
  return (
    <div className="flex flex-col items-center gap-3 rounded-xl border border-dashed border-border px-6 py-20 text-center">
      <span className="grid h-12 w-12 place-items-center rounded-xl bg-surface-muted text-foreground-muted">
        <Icon name="plug" />
      </span>
      <div>
        <p className="text-sm font-medium">Register your first MCP server</p>
        <p className="mx-auto mt-1 max-w-sm text-sm text-foreground-muted">
          Connect a remote Model Context Protocol server and Lumen will discover its tools so your
          assistants can use them — permissioned, tested, and scoped to you.
        </p>
      </div>
      <button
        type="button"
        onClick={onRegister}
        className="inline-flex items-center gap-1.5 rounded-md bg-accent px-3 py-1.5 text-sm font-medium text-white hover:opacity-90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
      >
        <Icon name="plus" className="shrink-0" />
        Register server
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
  // A 401 (expired/missing token, INV-4) is a re-auth dead-end a retry won't fix.
  const status = error instanceof ApiError ? error.status : 0;
  const unauthorized = status === 401;
  const message = unauthorized
    ? 'Your session expired. Sign in again to manage your MCP servers.'
    : error instanceof ApiError
      ? error.displayMessage || 'Could not load your MCP servers.'
      : 'Could not load your MCP servers.';

  return (
    <div
      role="alert"
      className="flex flex-col items-center gap-3 rounded-xl border border-danger/40 bg-danger/10 p-8 text-center"
    >
      <Icon name="alert-triangle" aria-hidden="true" />
      <p className="text-sm font-medium text-danger">Couldn’t load MCP servers</p>
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

function RegisterTile({ onRegister }: { onRegister: () => void }) {
  return (
    <button
      type="button"
      onClick={onRegister}
      className="flex h-full min-h-[13rem] w-full flex-col items-center justify-center gap-2 rounded-xl border border-dashed border-border bg-surface text-sm text-foreground-muted hover:border-accent/60 hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
    >
      <Icon name="plus" />
      Register a server
    </button>
  );
}
