/**
 * LedgerFooter (#121) — the tamper-evident footer from the audit wireframe.
 * Restates the audit trail's standing property: it is an append-only ledger
 * (spec 0004 §2.4 — events are recorded, never mutated). The left side counts
 * what's visible; the right side is the standing "Append-only ledger" status
 * (design-system `StatusDot`, ok tone).
 *
 * Honesty: it states only what the contract guarantees (append-only) — it does
 * NOT claim a "retained N days" SLA the MVP backend doesn't enforce.
 */
import { StatusDot } from '@/ui';

interface LedgerFooterProps {
  /** Events visible on this page after the client-side segment filter. */
  shown: number;
}

export function LedgerFooter({ shown }: LedgerFooterProps) {
  return (
    <div className="flex shrink-0 flex-wrap items-center justify-between gap-2 border-t border-border px-4 py-2 text-xs text-foreground-muted">
      <span>
        Showing {shown} event{shown === 1 ? '' : 's'} on this page · tamper-evident
      </span>
      <StatusDot tone="ok" label="Append-only ledger" title="Append-only ledger — events are never mutated" />
    </div>
  );
}
