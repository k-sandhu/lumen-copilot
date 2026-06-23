/**
 * ExportButton (#121) — a CLIENT-SIDE CSV export of the events currently on
 * screen. It serializes the page the api/ boundary already returned (no extra
 * request, no server export endpoint) and triggers a download via an object
 * URL. Disabled when there's nothing to export. The wireframe's "signed CSV"
 * affordance is intentionally a plain CSV here — the MVP backend exposes no
 * signing, so we don't claim one (issue #121 honesty guard).
 */
import { useState } from 'react';
import type { AuditEvent } from '@/api';
import { Icon } from '@/ui';
import { eventsToCsv } from '../model/metrics';

interface ExportButtonProps {
  events: readonly AuditEvent[];
}

/** Build a timestamped filename so repeated exports don't collide. */
function exportFilename(now: Date): string {
  const stamp = now.toISOString().slice(0, 19).replace(/[:T]/g, '-');
  return `lumen-audit-${stamp}.csv`;
}

export function ExportButton({ events }: ExportButtonProps) {
  const [busy, setBusy] = useState(false);
  const disabled = events.length === 0 || busy;

  const onExport = (): void => {
    if (events.length === 0) return;
    setBusy(true);
    try {
      const csv = eventsToCsv(events);
      const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = exportFilename(new Date());
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } finally {
      setBusy(false);
    }
  };

  return (
    <button
      type="button"
      onClick={onExport}
      disabled={disabled}
      className="inline-flex items-center gap-1.5 rounded-md border border-border bg-surface px-3 py-1.5 text-sm font-medium hover:bg-surface-muted disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
    >
      <Icon name="arrow-up-right" />
      Export CSV
    </button>
  );
}
