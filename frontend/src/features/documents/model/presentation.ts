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
