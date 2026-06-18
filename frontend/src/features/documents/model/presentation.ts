/**
 * Pure presentation helpers for the documents slice (#49) — no I/O, no React.
 * Keeps formatting logic out of JSX so it can be unit-tested directly.
 */
import type { StatusTone } from '@/components/StatusBadge';
import type { DocumentStatus } from '@/api';

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
