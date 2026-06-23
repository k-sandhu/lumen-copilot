/**
 * SourceCard (#27, ADR-0009) — one connector card in the grid, to the wireframe's
 * `.card.connector` layout (docs/wireframes/sources.html): a source glyph + name /
 * URL, a sync-health StatusDot + status badge, an indexed-object count, the
 * owner/permission (PermissionPill), the last-synced FreshnessPill, and the
 * per-source actions (re-sync + remove).
 *
 * Built from the production `@/ui` kit + Tailwind tokens (not the standalone
 * wireframe CSS) so it tracks the real design system, works light + dark, and
 * honors reduced-motion (the StatusDot pulse is a CSS animation the kit collapses
 * under prefers-reduced-motion). Long URLs truncate; the card never breaks layout.
 */
import { FreshnessPill, Icon, PermissionPill, StatusDot } from '@/ui';
import { cn } from '@/lib/cn';
import type { Source } from '../model/types';
import {
  freshness,
  modeLabel,
  sourceGlyph,
  sourceName,
  statusBadge,
  statusLabel,
  statusTone,
} from '../model/presentation';

interface SourceCardProps {
  source: Source;
  /** True while a re-sync mutation for THIS source is in flight. */
  syncing?: boolean;
  /** True while a remove mutation for THIS source is in flight. */
  removing?: boolean;
  onSync: (source: Source) => void;
  onRemove: (source: Source) => void;
}

export function SourceCard({ source, syncing, removing, onSync, onRemove }: SourceCardProps) {
  const tone = statusTone(source.status);
  const badge = statusBadge(source.status);
  const fresh = freshness(source);
  // The status is in-flight (a fresh sync was just enqueued) OR already syncing.
  const inFlight = syncing || source.status === 'syncing';
  const glyph = sourceGlyph(source);
  const name = sourceName(source);

  return (
    <article
      aria-label={`Source: ${name}`}
      data-status={source.status}
      className="flex min-w-0 flex-col gap-3 rounded-xl border border-border bg-surface p-4"
    >
      {/* Top: glyph + name/url + status badge */}
      <div className="flex min-w-0 items-start gap-3">
        <span
          aria-hidden="true"
          className="grid h-10 w-10 shrink-0 place-items-center rounded-lg bg-surface-muted text-xs font-bold text-foreground-muted"
        >
          {glyph}
        </span>
        <div className="min-w-0 grow">
          <h3 className="truncate text-sm font-semibold" title={name}>
            {name}
          </h3>
          <p className="truncate text-xs text-foreground-muted" title={source.config.url}>
            {source.config.url}
          </p>
        </div>
        <span className={cn('lc-badge shrink-0', badge.modifier)}>{badge.label}</span>
      </div>

      {/* Health line: status dot + freshness */}
      <div className="flex min-w-0 flex-wrap items-center gap-2 text-xs">
        <StatusDot tone={tone} label={statusLabel(source.status)} />
        {fresh ? <FreshnessPill label={fresh.label} stale={fresh.stale} /> : null}
      </div>

      {/* Error detail (only when the last sync failed) */}
      {source.status === 'error' && source.last_error ? (
        <p
          role="status"
          className="flex items-start gap-1.5 rounded-md bg-danger/10 px-2 py-1.5 text-xs text-danger"
        >
          <Icon name="alert-triangle" className="mt-px shrink-0" />
          <span className="min-w-0 break-words">{source.last_error}</span>
        </p>
      ) : null}

      {/* Stats: indexed count · mode · permission */}
      <dl className="grid grid-cols-3 gap-2 border-t border-border pt-3 text-xs">
        <div className="min-w-0">
          <dt className="text-foreground-muted">Indexed</dt>
          <dd className="mt-0.5 font-semibold tabular-nums">
            {source.status === 'pending' ? '—' : source.indexed_count.toLocaleString()}
          </dd>
        </div>
        <div className="col-span-2 min-w-0">
          <dt className="text-foreground-muted">Access</dt>
          <dd className="mt-0.5">
            <PermissionPill
              level="granted"
              label="Owner only"
              title="Only you (within your tenant) can retrieve this source"
            />
          </dd>
        </div>
      </dl>
      <p className="-mt-1 text-xs text-foreground-muted">{modeLabel(source)}</p>

      {/* Actions: re-sync + remove */}
      <div className="mt-auto flex items-center gap-2 pt-1">
        <button
          type="button"
          onClick={() => onSync(source)}
          disabled={inFlight || removing}
          className="inline-flex items-center gap-1.5 rounded-md border border-border px-2.5 py-1.5 text-xs font-medium hover:bg-surface-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-50"
        >
          <Icon name="sparkles" className="shrink-0" />
          {inFlight ? 'Syncing…' : 'Sync now'}
        </button>
        <button
          type="button"
          onClick={() => onRemove(source)}
          disabled={removing}
          aria-label={`Remove ${name}`}
          className="ml-auto inline-flex items-center gap-1.5 rounded-md border border-border px-2.5 py-1.5 text-xs font-medium text-danger hover:bg-danger/10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-danger disabled:opacity-50"
        >
          <Icon name="x" className="shrink-0" />
          Remove
        </button>
      </div>
    </article>
  );
}
