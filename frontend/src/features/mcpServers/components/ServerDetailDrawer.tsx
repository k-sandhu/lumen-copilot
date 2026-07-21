/**
 * ServerDetailDrawer (#228, ADR-0012) — a right-side drawer for one MCP server:
 * its health (StatusDot + status), last check, transport, endpoint, the masked
 * `secret_hint` (NEVER the stored value — AC-2 / CC-C #209), a "Test connection"
 * action (POST /mcp-servers/{id}/test surfacing the refreshed health + discovery
 * result), and the discovered tools from GET /mcp-servers/{id}/tools rendered with
 * a risk-tier badge each.
 *
 * Every async surface implements loading / empty / error+retry / success
 * (frontend/AGENTS.md): the detail read and the tools read each have their own
 * states, and a failed test surfaces the reason (the server never throws for a
 * down probe — it returns status: error, which we render as a clear health line).
 *
 * A11y: role="dialog" aria-modal, labelled by its title; focus moves into the
 * drawer on open and is restored on close; Escape and backdrop dismiss; the tools
 * list is a labelled list and the body scrolls independently.
 */
import { useId, useRef } from 'react';
import { ApiError } from '@/api';
import { Icon, RiskTierBadge, StatusDot } from '@/ui';
import { ScrollArea } from '@/components/ScrollArea';
import { useFocusTrap } from '@/lib/useFocusTrap';
import { useMcpServer, useMcpServerTools, useTestMcpServer } from '../model/queries';
import {
  endpointHost,
  lastCheckedLabel,
  serverErrorMessage,
  statusBadge,
  statusLabel,
  statusTone,
  transportLabel,
} from '../model/presentation';
import type { McpServer, McpTool } from '../model/types';

interface ServerDetailDrawerProps {
  /** The id of the server to show, or null when the drawer is closed. */
  serverId: string | null;
  onClose: () => void;
}

export function ServerDetailDrawer({ serverId, onClose }: ServerDetailDrawerProps) {
  const titleId = useId();
  const drawerRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);

  const open = serverId !== null;
  useFocusTrap(open, drawerRef, onClose, { initialFocus: closeRef });

  if (!open) return null;

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby={titleId}
      className="fixed inset-0 z-50 flex justify-end bg-black/50"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        ref={drawerRef}
        className="flex h-full w-full max-w-md flex-col border-l border-border bg-surface shadow-xl"
      >
        <header className="flex items-center gap-3 border-b border-border px-5 py-3.5">
          <h2 id={titleId} className="min-w-0 truncate text-sm font-semibold">
            Server details
          </h2>
          <button
            ref={closeRef}
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="ml-auto rounded-md border border-border p-1.5 text-foreground-muted hover:bg-surface-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
          >
            <Icon name="x" />
          </button>
        </header>

        <div className="min-h-0 flex-1">
          <ScrollArea viewportClassName="px-5 py-4">
            <DetailBody serverId={serverId} />
          </ScrollArea>
        </div>
      </div>
    </div>
  );
}

function DetailBody({ serverId }: { serverId: string }) {
  const detail = useMcpServer(serverId);
  const tools = useMcpServerTools(serverId);
  const test = useTestMcpServer(serverId);

  if (detail.isPending) return <DetailSkeleton />;
  if (detail.isError) {
    return (
      <ErrorState
        message={
          detail.error instanceof ApiError
            ? serverErrorMessage(detail.error)
            : 'Could not load this server.'
        }
        onRetry={
          detail.error instanceof ApiError && detail.error.status === 401
            ? undefined
            : () => void detail.refetch()
        }
        busy={detail.isFetching}
      />
    );
  }

  const server = detail.data;
  const testError = test.error instanceof ApiError ? test.error : null;

  return (
    <div className="space-y-5">
      <ServerSummary server={server} />

      {/* Test connection */}
      <div className="space-y-2">
        <button
          type="button"
          onClick={() => test.mutate()}
          disabled={test.isPending}
          className="inline-flex items-center gap-1.5 rounded-md bg-accent px-3 py-1.5 text-sm font-medium text-white hover:opacity-90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-60"
        >
          <Icon name="sparkles" className="shrink-0" />
          {test.isPending ? 'Testing…' : 'Test connection'}
        </button>
        {test.isSuccess ? (
          <p role="status" className="text-xs text-foreground-muted">
            {server.status === 'ready'
              ? `Healthy — discovered ${server.discovered_tool_count.toLocaleString()} tool${server.discovered_tool_count === 1 ? '' : 's'}.`
              : 'The probe finished — see the health status above.'}
          </p>
        ) : null}
        {testError ? (
          <p role="alert" className="flex items-start gap-1.5 text-xs text-danger">
            <Icon name="alert-triangle" className="mt-px shrink-0" />
            <span>{serverErrorMessage(testError)}</span>
          </p>
        ) : null}
      </div>

      {/* Discovered tools */}
      <section aria-labelledby={`${serverId}-tools`} className="space-y-2">
        <h3 id={`${serverId}-tools`} className="text-sm font-semibold">
          Discovered tools
        </h3>
        <ToolsBody
          tools={tools}
          onRetry={() => void tools.refetch()}
          serverPending={server.status === 'pending'}
        />
      </section>
    </div>
  );
}

function ServerSummary({ server }: { server: McpServer }) {
  const tone = statusTone(server.status);
  const badge = statusBadge(server.status);
  return (
    <div className="space-y-3 rounded-xl border border-border bg-surface-muted/40 p-4">
      <div className="flex min-w-0 items-start gap-2">
        <div className="min-w-0 grow">
          <h3 className="truncate text-sm font-semibold" title={server.name}>
            {server.name}
          </h3>
          <p className="truncate text-xs text-foreground-muted" title={server.endpoint_url}>
            {endpointHost(server)}
          </p>
        </div>
        <span className={`lc-badge shrink-0 ${badge.modifier}`}>{badge.label}</span>
      </div>

      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
        <StatusDot tone={tone} label={statusLabel(server.status)} />
        <span className="text-foreground-muted">{transportLabel(server.transport)}</span>
        <span className="text-foreground-muted">{lastCheckedLabel(server)}</span>
      </div>

      {server.status === 'error' && server.last_error ? (
        <p
          role="status"
          className="flex items-start gap-1.5 rounded-md bg-danger/10 px-2 py-1.5 text-xs text-danger"
        >
          <Icon name="alert-triangle" className="mt-px shrink-0" />
          <span className="min-w-0 break-words">{server.last_error}</span>
        </p>
      ) : null}

      <dl className="grid grid-cols-2 gap-3 border-t border-border pt-3 text-xs">
        <div className="min-w-0">
          <dt className="text-foreground-muted">Enabled</dt>
          <dd className="mt-0.5 font-medium">{server.enabled ? 'On' : 'Off'}</dd>
        </div>
        <div className="min-w-0">
          <dt className="text-foreground-muted">Secret</dt>
          {/* Masked hint ONLY — the stored value is write-only and never returned. */}
          <dd className="mt-0.5 font-medium">
            {server.secret_hint ? (
              <span className="lc-mono" title="Masked hint — the secret is never shown">
                {server.secret_hint}
              </span>
            ) : (
              <span className="text-foreground-muted">None</span>
            )}
          </dd>
        </div>
      </dl>
    </div>
  );
}

type ToolsQuery = ReturnType<typeof useMcpServerTools>;

function ToolsBody({
  tools,
  onRetry,
  serverPending,
}: {
  tools: ToolsQuery;
  onRetry: () => void;
  serverPending: boolean;
}) {
  if (tools.isPending) return <ToolsSkeleton />;
  if (tools.isError) {
    return (
      <ErrorState
        message={
          tools.error instanceof ApiError
            ? serverErrorMessage(tools.error)
            : 'Could not load the discovered tools.'
        }
        onRetry={
          tools.error instanceof ApiError && tools.error.status === 401 ? undefined : onRetry
        }
        busy={tools.isFetching}
      />
    );
  }

  const items = tools.data.items;
  if (items.length === 0) {
    return (
      <p className="rounded-lg border border-dashed border-border px-3 py-6 text-center text-xs text-foreground-muted">
        {serverPending
          ? 'No tools discovered yet — run “Test connection” to probe this server.'
          : 'This server advertised no tools on its last successful probe.'}
      </p>
    );
  }

  return (
    <ul aria-label="Discovered tools" className="space-y-2">
      {items.map((tool) => (
        <li key={tool.name}>
          <ToolRow tool={tool} />
        </li>
      ))}
    </ul>
  );
}

function ToolRow({ tool }: { tool: McpTool }) {
  return (
    <article className="space-y-1 rounded-lg border border-border bg-surface p-3">
      <div className="flex min-w-0 items-start gap-2">
        <h4 className="lc-mono min-w-0 grow break-all text-xs font-semibold" title={tool.name}>
          {tool.name}
        </h4>
        <RiskTierBadge tier={tool.risk_tier} className="shrink-0" />
      </div>
      {tool.description ? (
        <p className="text-xs text-foreground-muted">{tool.description}</p>
      ) : (
        <p className="text-xs italic text-foreground-muted">No description provided.</p>
      )}
    </article>
  );
}

function DetailSkeleton() {
  return (
    <div role="status" aria-busy="true" aria-live="polite" className="space-y-5">
      <span className="sr-only">Loading server details…</span>
      <div className="h-40 rounded-xl border border-border bg-surface p-4" aria-hidden="true">
        <div className="lc-skeleton" style={{ width: '55%' }} />
        <div className="lc-skeleton" style={{ width: '35%', marginTop: 10 }} />
        <div className="lc-skeleton" style={{ width: '100%', marginTop: 20, height: 40 }} />
      </div>
      <ToolsSkeleton />
    </div>
  );
}

function ToolsSkeleton() {
  return (
    <div role="status" aria-busy="true" aria-live="polite" className="space-y-2">
      <span className="sr-only">Loading discovered tools…</span>
      {[0, 1, 2].map((i) => (
        <div
          key={i}
          className="h-16 rounded-lg border border-border bg-surface p-3"
          aria-hidden="true"
        >
          <div className="lc-skeleton" style={{ width: '50%' }} />
          <div className="lc-skeleton" style={{ width: '80%', marginTop: 8 }} />
        </div>
      ))}
    </div>
  );
}

function ErrorState({
  message,
  onRetry,
  busy,
}: {
  message: string;
  onRetry?: () => void;
  busy: boolean;
}) {
  return (
    <div
      role="alert"
      className="flex flex-col items-center gap-3 rounded-xl border border-danger/40 bg-danger/10 p-6 text-center"
    >
      <Icon name="alert-triangle" aria-hidden="true" />
      <p className="max-w-sm text-sm text-foreground-muted">{message}</p>
      {onRetry ? (
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
