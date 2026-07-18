/**
 * SourceCard (#27, ADR-0009; #455, ADR-0019) — one connector card in the grid,
 * to the wireframe's `.card.connector` layout (docs/wireframes/sources.html): a
 * source glyph + name / detail line, a sync-health StatusDot + status badge, an
 * indexed-object count, the permission (PermissionPill), the last-synced
 * FreshnessPill, and the per-source actions (re-sync + remove).
 *
 * A managed `gdrive` source additionally renders the ADR-0019 §5 health
 * surface: the connected account email, the mirrored-ACL freshness
 * (`acl_synced_at` — stale ACLs DENY at retrieval, so it is a trust signal),
 * the `unmapped_acl_count` with the identity-attestation hint, and the
 * **Reauthorize** action when the OAuth grant died (`reauthorize_required`).
 * A `pending_auth` source offers **Connect** to (re)start the consent flow.
 *
 * ADMIN GATING (INV-5, the #455 negative AC): every managed-source mutation is
 * tenant-admin-only, so for a non-admin a gdrive card renders NO affordances —
 * no sync / remove / connect / reauthorize — just read-only health. `web`
 * cards keep their owner-scoped actions for everyone.
 */
import { FreshnessPill, Icon, PermissionPill, StatusDot } from '@/ui';
import { cn } from '@/lib/cn';
import type { ApiError } from '@/api';
import type { Source } from '../model/types';
import {
  aclFreshness,
  freshness,
  isGdriveSource,
  modeLabel,
  sourceDetail,
  sourceGlyph,
  sourceName,
  statusBadge,
  statusLabel,
  statusTone,
} from '../model/presentation';

interface SourceCardProps {
  source: Source;
  /** Whether the CALLER holds the tenant-admin role (gates managed-source actions). */
  isAdmin?: boolean;
  /** True while a re-sync mutation for THIS source is in flight. */
  syncing?: boolean;
  /** Last failed re-sync trigger for THIS source. */
  syncError?: ApiError | null;
  /** True while a connect/reauthorize mutation for THIS source is in flight. */
  connecting?: boolean;
  /** Last failed connect/reauthorize trigger for THIS source. */
  connectError?: ApiError | null;
  /** True while a remove mutation for THIS source is in flight. */
  removing?: boolean;
  onSync: (source: Source) => void;
  onRemove: (source: Source) => void;
  /** Start (or restart) the OAuth consent flow for a managed source. */
  onConnect?: (source: Source) => void;
}

export function SourceCard({
  source,
  isAdmin = false,
  syncing,
  syncError,
  connecting,
  connectError,
  removing,
  onSync,
  onRemove,
  onConnect,
}: SourceCardProps) {
  const tone = statusTone(source.status);
  const badge = statusBadge(source.status);
  const fresh = freshness(source);
  // The status is in-flight (a fresh sync was just enqueued) OR already syncing.
  const inFlight = syncing || source.status === 'syncing';
  const glyph = sourceGlyph(source);
  const name = sourceName(source);
  const detail = sourceDetail(source);

  const managed = isGdriveSource(source);
  // Managed-source mutations are admin-gated at action time (ADR-0019 §1, INV-5):
  // a non-admin gets NO affordances on a gdrive card.
  const canMutate = !managed || isAdmin;
  const awaitingConsent = source.status === 'pending_auth';
  const needsReauthorize = managed && source.reauthorize_required;
  const showConnect = managed && canMutate && (awaitingConsent || needsReauthorize);

  return (
    <article
      aria-label={`Source: ${name}`}
      data-status={source.status}
      className="flex min-w-0 flex-col gap-3 rounded-xl border border-border bg-surface p-4"
    >
      {/* Top: glyph + name/detail + status badge */}
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
          <p className="truncate text-xs text-foreground-muted" title={detail}>
            {detail}
          </p>
        </div>
        <span className={cn('lc-badge shrink-0', badge.modifier)}>{badge.label}</span>
      </div>

      {/* Health line: status dot + freshness (+ mirrored-ACL freshness when managed) */}
      <div className="flex min-w-0 flex-wrap items-center gap-2 text-xs">
        <StatusDot tone={tone} label={statusLabel(source.status)} />
        {fresh ? <FreshnessPill label={fresh.label} stale={fresh.stale} /> : null}
        {managed ? <AclFreshness source={source} /> : null}
      </div>

      {/* Reauthorize warning (a dead OAuth grant — re-consent repairs it) */}
      {needsReauthorize ? (
        <p
          role="status"
          className="flex items-start gap-1.5 rounded-md bg-warn/10 px-2 py-1.5 text-xs text-warn"
        >
          <Icon name="alert-triangle" className="mt-px shrink-0" />
          <span className="min-w-0 break-words">
            Google access expired or was revoked — reauthorize to resume syncing.
          </span>
        </p>
      ) : null}

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

      {/* Stats: indexed count · permission */}
      <dl className="grid grid-cols-3 gap-2 border-t border-border pt-3 text-xs">
        <div className="min-w-0">
          <dt className="text-foreground-muted">Indexed</dt>
          <dd className="mt-0.5 font-semibold tabular-nums">
            {source.status === 'pending' || source.status === 'pending_auth'
              ? '—'
              : source.indexed_count.toLocaleString()}
          </dd>
        </div>
        <div className="col-span-2 min-w-0">
          <dt className="text-foreground-muted">Access</dt>
          <dd className="mt-0.5">
            {managed ? (
              <PermissionPill
                level="restricted"
                label="Source permissions"
                title="Access mirrors Google Drive's own file permissions — never widened"
              />
            ) : (
              <PermissionPill
                level="granted"
                label="Owner only"
                title="Only you (within your tenant) can retrieve this source"
              />
            )}
          </dd>
        </div>
      </dl>

      {/* Managed health: connected account + unmapped-ACL count */}
      {managed ? (
        <dl className="grid grid-cols-1 gap-1.5 text-xs">
          <div className="flex min-w-0 items-baseline gap-2">
            <dt className="shrink-0 text-foreground-muted">Connected as</dt>
            <dd className="min-w-0 truncate font-medium" title={source.connected_account?.email}>
              {source.connected_account ? source.connected_account.email : 'Not connected yet'}
            </dd>
          </div>
          {/* The unmapped-ACL health state is a REQUIRED contract field — always
              rendered with an explicit state, never hidden (0 and null are
              meaningful health readings, not absences). */}
          <div className="flex min-w-0 items-start gap-2">
            <dt className="shrink-0 text-foreground-muted">Unmapped access</dt>
            <dd className="min-w-0">
              {source.unmapped_acl_count === null ? (
                'Not available until the first permissions sync.'
              ) : source.unmapped_acl_count === 0 ? (
                'None — every mirrored permission maps to a member.'
              ) : (
                <>
                  <span className="font-medium tabular-nums">
                    {source.unmapped_acl_count.toLocaleString()}
                  </span>{' '}
                  document{source.unmapped_acl_count === 1 ? '' : 's'} visible to no one yet —
                  attesting member identities in Admin lights them up.
                </>
              )}
            </dd>
          </div>
        </dl>
      ) : null}
      <p className="-mt-1 text-xs text-foreground-muted">{modeLabel(source)}</p>

      {/* Actions — none at all for a non-admin on a managed source (INV-5) */}
      {canMutate ? (
        <div className="mt-auto flex items-center gap-2 pt-1">
          {showConnect ? (
            <button
              type="button"
              onClick={() => onConnect?.(source)}
              disabled={connecting || removing}
              className="inline-flex items-center gap-1.5 rounded-md bg-accent px-2.5 py-1.5 text-xs font-medium text-white hover:opacity-90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-50"
            >
              <Icon name="plug" className="shrink-0" />
              {connecting ? 'Starting…' : needsReauthorize ? 'Reauthorize' : 'Connect'}
            </button>
          ) : (
            <button
              type="button"
              onClick={() => onSync(source)}
              disabled={inFlight || removing}
              className="inline-flex items-center gap-1.5 rounded-md border border-border px-2.5 py-1.5 text-xs font-medium hover:bg-surface-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-50"
            >
              <Icon name="sparkles" className="shrink-0" />
              {inFlight ? 'Syncing…' : 'Sync now'}
            </button>
          )}
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
      ) : null}
      {syncError ? (
        <p role="alert" className="mt-1 text-xs text-danger">
          {syncError.displayMessage}
        </p>
      ) : null}
      {connectError ? (
        <p role="alert" className="mt-1 text-xs text-danger">
          {connectError.displayMessage}
        </p>
      ) : null}
    </article>
  );
}

/** The mirrored-ACL freshness pill (managed sources only). */
function AclFreshness({ source }: { source: Extract<Source, { type: 'gdrive' }> }) {
  const acl = aclFreshness(source);
  return <FreshnessPill label={acl.label} stale={acl.stale} title={source.acl_synced_at ?? undefined} />;
}
