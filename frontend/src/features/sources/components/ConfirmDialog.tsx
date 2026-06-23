/**
 * ConfirmDialog (#27) — a small modal confirm for the destructive remove action.
 * Removing a source cascades its ingested documents (ADR-0009 §5), so it gates
 * behind an explicit confirm (read-before-write spirit, AGENTS.md §2.3).
 *
 * A11y: role="alertdialog" aria-modal, labelled + described; focus moves to the
 * confirm button on open and is restored on close; Escape and backdrop dismiss.
 */
import { useEffect, useId, useRef } from 'react';

interface ConfirmDialogProps {
  open: boolean;
  title: string;
  description: string;
  confirmLabel: string;
  /** Disable buttons + show a pending label while the action runs. */
  busy?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

export function ConfirmDialog({
  open,
  title,
  description,
  confirmLabel,
  busy = false,
  onConfirm,
  onCancel,
}: ConfirmDialogProps) {
  const titleId = useId();
  const descId = useId();
  const confirmRef = useRef<HTMLButtonElement>(null);
  const lastFocused = useRef<HTMLElement | null>(null);

  useEffect(() => {
    if (!open) return;
    lastFocused.current = (document.activeElement as HTMLElement) ?? null;
    const id = window.requestAnimationFrame(() => confirmRef.current?.focus());
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape' && !busy) onCancel();
    }
    window.addEventListener('keydown', onKey);
    return () => {
      window.cancelAnimationFrame(id);
      window.removeEventListener('keydown', onKey);
      lastFocused.current?.focus?.();
    };
  }, [open, busy, onCancel]);

  if (!open) return null;

  return (
    <div
      role="alertdialog"
      aria-modal="true"
      aria-labelledby={titleId}
      aria-describedby={descId}
      className="fixed inset-0 z-[60] flex items-center justify-center bg-black/50 p-4"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget && !busy) onCancel();
      }}
    >
      <div className="w-full max-w-sm overflow-hidden rounded-xl border border-border bg-surface shadow-xl">
        <div className="space-y-2 px-5 py-4">
          <h2 id={titleId} className="text-sm font-semibold">
            {title}
          </h2>
          <p id={descId} className="text-sm text-foreground-muted">
            {description}
          </p>
        </div>
        <div className="flex items-center justify-end gap-2 border-t border-border px-5 py-3">
          <button
            type="button"
            onClick={onCancel}
            disabled={busy}
            className="rounded-md border border-border px-3 py-1.5 text-sm hover:bg-surface-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-50"
          >
            Cancel
          </button>
          <button
            ref={confirmRef}
            type="button"
            onClick={onConfirm}
            disabled={busy}
            className="rounded-md bg-danger px-3 py-1.5 text-sm font-medium text-white hover:opacity-90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-danger disabled:opacity-60"
          >
            {busy ? 'Removing…' : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
