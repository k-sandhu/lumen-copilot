/**
 * Presentation mapping for the audit slice (#86). Bridges the FROZEN wire shape
 * (api/types `AuditEvent`, spec 0004 §2.4) to the design-system kit shapes
 * (`ui/AuditRow` `AuditEvent`, `ui/ProvenanceDrawer` `ProvenanceDetail`). Pure —
 * no I/O, no React — so it is trivially unit-testable and reusable.
 *
 * The wire event taxonomy (`event_type`) is broader than the kit's four visual
 * `AuditKind`s; we fold each event_type into one kind so every row gets the right
 * icon (retrieval / answer / access-decision / action), per the issue's three
 * row classes plus the `action.*` group.
 */
import type { AuditEvent as KitAuditEvent, AuditKind } from '@/ui';
import type { ProvenanceCandidate, ProvenanceDetail } from '@/ui';
import type { AuditEvent, AuditEventType } from '@/api';

/** Fold the wire `event_type` taxonomy into one of the kit's visual kinds. */
export function kindForEventType(type: AuditEventType): AuditKind {
  switch (type) {
    case 'retrieval.query':
      return 'retrieval';
    case 'answer.generated':
      return 'answer';
    case 'auth.login':
    case 'auth.login_failed':
    case 'auth.logout':
    case 'permission.denied':
    case 'document.viewed':
    case 'document.downloaded':
      return 'access';
    case 'collection.created':
    case 'document.uploaded':
    case 'document.deleted':
    case 'action.requested':
    case 'action.approved':
    case 'action.executed':
      return 'action';
    default:
      return 'access';
  }
}

/** Human-readable label for a wire event_type (for the row's primary line). */
export function eventTypeLabel(type: AuditEventType): string {
  const LABELS: Record<AuditEventType, string> = {
    'auth.login': 'Signed in',
    'auth.login_failed': 'Sign-in failed',
    'auth.logout': 'Signed out',
    'collection.created': 'Created collection',
    'document.uploaded': 'Uploaded document',
    'document.viewed': 'Viewed document',
    'document.downloaded': 'Downloaded document',
    'document.deleted': 'Deleted document',
    'retrieval.query': 'Retrieval',
    'answer.generated': 'Generated answer',
    'permission.denied': 'Permission denied',
    'action.requested': 'Action requested',
    'action.approved': 'Action approved',
    'action.executed': 'Action executed',
  };
  return LABELS[type] ?? type;
}

/** A short id for monospace display, e.g. "evt_9f3a…" — never the full UUID. */
export function shortId(id: string): string {
  return id.length > 12 ? `${id.slice(0, 12)}…` : id;
}

/** Format a UTC ISO-8601 timestamp as a compact local time for the row. */
export function formatTime(ts: string): string {
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return ts;
  return d.toLocaleString(undefined, {
    month: 'short',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  });
}

/** Map a wire decision to a glanceable verb for the row's secondary line. */
function decisionLabel(decision: AuditEvent['decision']): string {
  switch (decision) {
    case 'allowed':
      return 'allowed';
    case 'denied':
      return 'denied';
    case 'error':
      return 'error';
  }
}

/** Wire event → the kit `AuditRow` shape (icon, primary, secondary, time). */
export function toKitRow(event: AuditEvent): KitAuditEvent {
  const detailParts = [event.actor, decisionLabel(event.decision)];
  if (event.resource_id) detailParts.push(event.resource_id);
  return {
    id: shortId(event.id),
    kind: kindForEventType(event.event_type),
    action: eventTypeLabel(event.event_type),
    detail: detailParts.join(' · '),
    time: formatTime(event.ts),
  };
}

/** Wire candidate → the kit `ProvenanceCandidate` (allow/exclude + reason). */
function toKitCandidate(c: AuditEvent['provenance']['candidates'][number]): ProvenanceCandidate {
  return {
    title: c.resource_id,
    decision: c.disposition === 'allow' ? 'allowed' : 'excluded',
    reason: typeof c.score === 'number' ? `${c.reason} · score ${c.score.toFixed(3)}` : c.reason,
  };
}

/**
 * Wire event → the kit `ProvenanceDrawer` detail: ordered metadata, the
 * per-candidate allow/exclude ledger (INCLUDING excluded candidates, so a
 * permission trim is provable after the fact — mission filter #4), and the raw
 * recorded payload (rendered monospace by the drawer).
 */
export function toProvenanceDetail(event: AuditEvent): ProvenanceDetail {
  const meta: Array<{ key: string; value: string }> = [
    { key: 'Event', value: event.event_type },
    { key: 'When', value: formatTime(event.ts) },
    { key: 'Actor', value: event.actor },
    { key: 'Tenant', value: event.tenant_id },
    { key: 'Decision', value: decisionLabel(event.decision) },
  ];
  if (event.resource_id) meta.push({ key: 'Resource', value: event.resource_id });

  return {
    id: event.id,
    meta,
    candidates: event.provenance.candidates.map(toKitCandidate),
    // The raw recorded payload, pretty-printed monospace by the drawer.
    raw: event.provenance.raw ?? {},
  };
}
