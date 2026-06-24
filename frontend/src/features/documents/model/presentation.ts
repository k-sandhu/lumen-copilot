/**
 * Pure presentation helpers for the documents slice (#49, extended for the #89
 * trust-signal re-skin) — no I/O, no React. Keeps formatting logic out of JSX so
 * it can be unit-tested directly.
 */
import type { StatusTone } from '@/components/StatusBadge';
import type { StatusTone as DotTone } from '@/ui';
import type { Document, DocumentStatus } from '@/api';

/** Map an ingestion status to a status-badge tone. */
export function statusTone(status: DocumentStatus): StatusTone {
  switch (status) {
    case 'ready':
      return 'ok';
    case 'failed':
      return 'danger';
    case 'pending':
    case 'processing':
      return 'pending';
  }
}

/** Human label for a status. */
export function statusLabel(status: DocumentStatus): string {
  switch (status) {
    case 'pending':
      return 'Queued';
    case 'processing':
      return 'Processing';
    case 'ready':
      return 'Ready';
    case 'failed':
      return 'Failed';
  }
}

/** True while a document is still being ingested (drives polling + spinners). */
export function isIngesting(status: DocumentStatus): boolean {
  return status === 'pending' || status === 'processing';
}

/** Compact human-readable byte size (e.g. "1.2 MB"). */
export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return '—';
  if (bytes < 1024) return `${bytes} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  const rounded = value >= 10 ? Math.round(value) : Math.round(value * 10) / 10;
  return `${rounded} ${units[unit]}`;
}

// --- #89 trust-signal re-skin ----------------------------------------------

/** Map an ingestion status to a kit StatusDot tone (drives the sync pulse). */
export function statusDotTone(status: DocumentStatus): DotTone {
  switch (status) {
    case 'ready':
      return 'ok';
    case 'failed':
      return 'danger';
    case 'processing':
      return 'sync';
    case 'pending':
      return 'muted';
  }
}

/** A short type tag from a filename / mime (e.g. "PDF", "DOCX"). */
export function fileKind(doc: Pick<Document, 'filename' | 'mime_type'>): string {
  const ext = doc.filename.includes('.') ? doc.filename.split('.').pop() : undefined;
  if (ext) return ext.toUpperCase();
  const subtype = doc.mime_type.split('/').pop() ?? '';
  return (subtype || 'FILE').toUpperCase();
}

// --- #119 documents-table polish -------------------------------------------

/** Visual family of a file-type badge (drives its tint in the table). */
export type FileKindTone = 'pdf' | 'doc' | 'sheet' | 'slide' | 'image' | 'text' | 'default';

/**
 * Group a file kind into a small visual family so the type badge reads at a
 * glance (PDF red, doc blue, sheet green, slide orange, …) — mirrors the
 * wireframe `.src-*` swatches but maps onto our semantic tokens, not the
 * wireframe gradients.
 */
export function fileKindTone(doc: Pick<Document, 'filename' | 'mime_type'>): FileKindTone {
  const kind = fileKind(doc).toLowerCase();
  const mime = doc.mime_type.toLowerCase();
  if (kind === 'pdf' || mime.includes('pdf')) return 'pdf';
  if (['doc', 'docx', 'rtf', 'odt'].includes(kind) || mime.includes('word')) return 'doc';
  if (['xls', 'xlsx', 'csv', 'ods'].includes(kind) || mime.includes('sheet') || mime.includes('csv'))
    return 'sheet';
  if (['ppt', 'pptx', 'odp'].includes(kind) || mime.includes('presentation')) return 'slide';
  if (['png', 'jpg', 'jpeg', 'gif', 'webp', 'svg'].includes(kind) || mime.startsWith('image/'))
    return 'image';
  if (['md', 'markdown', 'txt'].includes(kind) || mime.startsWith('text/')) return 'text';
  return 'default';
}

/**
 * Relative-time label from an ISO stamp, e.g. "2h ago" / "3d ago" / "just now".
 * `now` is injectable so unit tests stay deterministic. Returns `null` for a
 * missing/unparseable stamp. (Mirrors the sources slice's helper — no shared
 * cross-feature import per frontend/AGENTS.md.)
 */
export function relativeTime(
  iso: string | null | undefined,
  now: number = Date.now(),
): string | null {
  if (!iso) return null;
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return null;
  const seconds = Math.round((now - then) / 1000);
  if (seconds < 45) return 'just now';
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.round(hours / 24);
  if (days < 30) return `${days}d ago`;
  const months = Math.round(days / 30);
  if (months < 12) return `${months}mo ago`;
  return `${Math.round(months / 12)}y ago`;
}

/** A `updated_at` older than this reads as stale (amber) in the table. */
export const DOC_STALE_AFTER_MS = 30 * 24 * 60 * 60 * 1000;

/**
 * Freshness for the Updated column, derived from the real `updated_at` stamp —
 * a label + a stale flag (past the freshness window). Returns `null` only when
 * the stamp is missing/unparseable.
 */
export function documentFreshness(
  doc: Pick<Document, 'updated_at'>,
  now: number = Date.now(),
): { label: string; stale: boolean; title: string } | null {
  const rel = relativeTime(doc.updated_at, now);
  if (!rel) return null;
  const then = Date.parse(doc.updated_at);
  const stale = !Number.isNaN(then) && now - then > DOC_STALE_AFTER_MS;
  return { label: rel, stale, title: doc.updated_at };
}

/**
 * Owner label for the Owner column. The contract only carries an `owner_id`
 * (UUID) — there is NO display-name field — so we render "You" for the current
 * user and a short, honest id fragment otherwise. We never fabricate a person's
 * name (scope guard: no backend-unsupported data).
 */
export function ownerLabel(
  doc: Pick<Document, 'owner_id'>,
  currentUserId: string | undefined,
): string {
  if (currentUserId && doc.owner_id === currentUserId) return 'You';
  const id = doc.owner_id ?? '';
  const short = id.includes('-') ? id.split('-')[0] : id.slice(0, 8);
  return short ? `User ${short}` : 'Unknown';
}

/**
 * Visibility for the Visibility column, expressed as kit-PermissionPill props.
 *
 * The MVP backend has NO per-document visibility taxonomy (no Confidential /
 * Team / Org / Private field on the wire). The authoritative invariant is
 * spec 0004 INV-2: an uploaded document is retrievable only by its owner within
 * the tenant. So we render that real invariant honestly — "Private to you" for
 * your own documents, "Owner only" for another user's — rather than inventing
 * the wireframe's mock visibility levels.
 */
export function visibility(
  doc: Pick<Document, 'owner_id'>,
  currentUserId: string | undefined,
): { level: 'granted' | 'restricted'; label: string; title: string } {
  const mine = Boolean(currentUserId) && doc.owner_id === currentUserId;
  return mine
    ? {
        level: 'granted',
        label: 'Private to you',
        title: 'Only you, within your tenant, can retrieve this document (INV-2)',
      }
    : {
        level: 'restricted',
        label: 'Owner only',
        title: 'Retrievable only by its owner within the tenant (INV-2)',
      };
}

/** One step of the parse → chunk → embed → ready ingestion pipeline. */
export interface IngestStep {
  /** Stable key for the stage. */
  key: 'parse' | 'chunk' | 'embed' | 'ready';
  label: string;
  /** Where this stage stands given the document's current status. */
  state: 'done' | 'active' | 'pending' | 'failed';
}

/**
 * The ingestion trace for a document (parse → chunk → embed → ready), derived
 * from its single `status` field + `chunk_count` — the only ingestion signal the
 * contract exposes (no new wire data). We don't get per-stage status on the
 * wire, so we infer a faithful, monotonic projection:
 *   - pending     → nothing started yet (parse is the active/next stage)
 *   - processing  → parse+chunk done, embed in flight (chunk shown once counted)
 *   - ready       → all stages done
 *   - failed      → the first not-yet-complete stage is marked failed
 */
export function ingestSteps(doc: Pick<Document, 'status' | 'chunk_count'>): IngestStep[] {
  const chunked = doc.chunk_count > 0;
  const labels: Record<IngestStep['key'], string> = {
    parse: 'Parsed',
    chunk: chunked ? `Chunked into ${doc.chunk_count} passages` : 'Chunked into passages',
    embed: 'Embedded',
    ready: 'Indexed & permission-scoped',
  };

  // Per-status completion frontier: how many stages are fully done.
  // pending: 0 · processing: parse done (+chunk if counted) · ready: all.
  let doneThrough: number;
  switch (doc.status) {
    case 'pending':
      doneThrough = 0;
      break;
    case 'processing':
      doneThrough = chunked ? 2 : 1;
      break;
    case 'ready':
      doneThrough = 4;
      break;
    case 'failed':
      doneThrough = chunked ? 2 : 0;
      break;
  }

  const keys: IngestStep['key'][] = ['parse', 'chunk', 'embed', 'ready'];
  return keys.map((key, i) => {
    let state: IngestStep['state'];
    if (i < doneThrough) {
      state = 'done';
    } else if (doc.status === 'failed' && i === doneThrough) {
      state = 'failed';
    } else if (doc.status === 'processing' && i === doneThrough) {
      state = 'active';
    } else {
      state = 'pending';
    }
    return { key, label: labels[key], state };
  });
}
