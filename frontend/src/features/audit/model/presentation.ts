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
      // The taxonomy has 84 actions and only ever grows (#545), so an exhaustive
      // switch would be a standing merge conflict. Fold the rest by VERB: anything
      // that changed state is an action, anything that read or was refused is an
      // access decision. Getting this wrong picks a slightly-off icon — it never
      // hides a row — so a heuristic is the right trade here.
      return /\.(created|updated|deleted|added|removed|revoked|granted|executed|reset|closed|connected|synced|published|drafted|certified|deprecated|disabled|featured|transferred|attested|accessed|requested|approved)$/.test(
        type,
      )
        ? 'action'
        : 'access';
  }
}

/**
 * Turn a wire action into a readable sentence when no curated label exists —
 * `sandbox_session.created` reads as "Sandbox session created".
 *
 * The alternative was showing the raw dotted identifier in the ledger, which is
 * what happened before: `eventTypeLabel` fell back to `?? type`, and the curated
 * map covered 14 of 84 actions.
 */
function humanise(type: string): string {
  const words = type.replace(/[._]/g, ' ').trim();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/** Human-readable label for a wire event_type (for the row's primary line). */
export function eventTypeLabel(type: AuditEventType): string {
  // PARTIAL by design: these are the hand-polished wordings, not the taxonomy.
  // A total map would break the build every time an action is added — which is how
  // this drifted to 14 of 84 in the first place — so anything uncurated falls back
  // to a readable rendering of the action itself.
  const LABELS: Partial<Record<AuditEventType, string>> = {
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
  return LABELS[type] ?? humanise(type);
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
