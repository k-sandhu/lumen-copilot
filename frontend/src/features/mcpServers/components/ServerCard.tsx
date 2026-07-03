/**
 * ServerCard (#228, ADR-0012) — one MCP server card in the grid: a glyph + name /
 * endpoint host, the transport, a health StatusDot + status badge, an enabled
 * toggle, the discovered-tool count, and the per-server actions (Test connection,
 * View tools/details, Remove). Mirrors the Sources connector-card UX (SourceCard).
 *
 * A downed server (status: error) surfaces its `last_error` inline in a danger
 * panel — never a blank card (frontend/AGENTS.md "every state, not just
 * success"). Long endpoints truncate; the card never breaks layout. Built from
 * the production `@/ui` kit so it tracks the design system and honors
 * prefers-reduced-motion.
 */
import { Icon, StatusDot } from '@/ui';
import { cn } from '@/lib/cn';
import type { ApiError } from '@/api';
import type { McpServer } from '../model/types';
import {
  endpointHost,
  lastCheckedLabel,
  serverGlyph,
  statusBadge,
  statusLabel,
  statusTone,
  transportLabel,
} from '../model/presentation';

interface ServerCardProps {
  server: McpServer;
  /** True while a test-connection mutation for THIS server is in flight. */
  testing?: boolean;
  /** Last failed test trigger for THIS server. */
  testError?: ApiError | null;
  /** True while an enable/disable toggle for THIS server is in flight. */
  toggling?: boolean;
  /** True while a remove mutation for THIS server is in flight. */
  removing?: boolean;
  onTest: (server: McpServer) => void;
  onToggle: (server: McpServer) => void;
  onOpen: (server: McpServer) => void;
  onRemove: (server: McpServer) => void;
}

export function ServerCard({
  server,
  testing,
  testError,
  toggling,
  removing,
  onTest,
  onToggle,
  onOpen,
  onRemove,
}: ServerCardProps) {
  const tone = statusTone(server.status);
  const badge = statusBadge(server.status);
  const glyph = serverGlyph(server);
  const host = endpointHost(server);
  const busy = testing || toggling || removing;

  return (
    <article
      aria-label={`MCP server: ${server.name}`}
      data-status={server.status}
      className="flex min-w-0 flex-col gap-3 rounded-xl border border-border bg-surface p-4"
    >
      {/* Top: glyph + name/host + status badge */}
      <div className="flex min-w-0 items-start gap-3">
        <span
          aria-hidden="true"
          className="grid h-10 w-10 shrink-0 place-items-center rounded-lg bg-surface-muted text-xs font-bold text-foreground-muted"
        >
          {glyph}
        </span>
        <div className="min-w-0 grow">
          <h3 className="truncate text-sm font-semibold" title={server.name}>
            {server.name}
          </h3>
          <p className="truncate text-xs text-foreground-muted" title={server.endpoint_url}>
            {host}
          </p>
        </div>
        <span className={cn('lc-badge shrink-0', badge.modifier)}>{badge.label}</span>
      </div>

      {/* Health line: status dot + transport + last checked */}
      <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 text-xs">
        <StatusDot tone={tone} label={statusLabel(server.status)} />
        <span className="text-foreground-muted">{transportLabel(server.transport)}</span>
        <span className="text-foreground-muted">{lastCheckedLabel(server)}</span>
      </div>

      {/* Error detail (only when the last probe failed) */}
      {server.status === 'error' && server.last_error ? (
        <p
          role="status"
          className="flex items-start gap-1.5 rounded-md bg-danger/10 px-2 py-1.5 text-xs text-danger"
        >
          <Icon name="alert-triangle" className="mt-px shrink-0" />
          <span className="min-w-0 break-words">{server.last_error}</span>
        </p>
      ) : null}

      {/* Stats: discovered tools · enabled toggle */}
      <dl className="grid grid-cols-2 gap-2 border-t border-border pt-3 text-xs">
        <div className="min-w-0">
          <dt className="text-foreground-muted">Discovered tools</dt>
          <dd className="mt-0.5 font-semibold tabular-nums">
            {server.status === 'pending' ? '—' : server.discovered_tool_count.toLocaleString()}
          </dd>
        </div>
        <div className="min-w-0">
          <dt className="text-foreground-muted">Enabled</dt>
          <dd className="mt-0.5">
            <label className="inline-flex cursor-pointer items-center gap-2">
              <input
                type="checkbox"
                role="switch"
                checked={server.enabled}
                disabled={busy}
                aria-label={`${server.enabled ? 'Disable' : 'Enable'} ${server.name}`}
                onChange={() => onToggle(server)}
                className="h-4 w-4 accent-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-50"
              />
              <span className="font-medium">
                {toggling ? 'Saving…' : server.enabled ? 'On' : 'Off'}
              </span>
            </label>
          </dd>
        </div>
      </dl>

      {/* Actions: test + view details + remove */}
      <div className="mt-auto flex flex-wrap items-center gap-2 pt-1">
        <button
          type="button"
          onClick={() => onTest(server)}
          disabled={busy}
          className="inline-flex items-center gap-1.5 rounded-md border border-border px-2.5 py-1.5 text-xs font-medium hover:bg-surface-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-50"
        >
          <Icon name="sparkles" className="shrink-0" />
          {testing ? 'Testing…' : 'Test connection'}
        </button>
        <button
          type="button"
          onClick={() => onOpen(server)}
          className="inline-flex items-center gap-1.5 rounded-md border border-border px-2.5 py-1.5 text-xs font-medium hover:bg-surface-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
        >
          <Icon name="list" className="shrink-0" />
          Details
        </button>
        <button
          type="button"
          onClick={() => onRemove(server)}
          disabled={removing}
          aria-label={`Remove ${server.name}`}
          className="ml-auto inline-flex items-center gap-1.5 rounded-md border border-border px-2.5 py-1.5 text-xs font-medium text-danger hover:bg-danger/10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-danger disabled:opacity-50"
        >
          <Icon name="trash" className="shrink-0" />
          Remove
        </button>
      </div>
      {testError ? (
        <p role="alert" className="mt-1 text-xs text-danger">
          {testError.displayMessage}
        </p>
      ) : null}
    </article>
  );
}
