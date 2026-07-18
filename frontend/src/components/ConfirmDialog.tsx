/**
 * ConfirmDialog (#27) — a small modal confirm for destructive actions. Shared
 * across features (Sources remove, Documents/Collections delete; #166) — the one
 * focus-managed confirm the app standardizes on, so destructive deletes don't fall
 * back to native `window.confirm`.
 *
 * A11y: role="alertdialog" aria-modal, labelled + described; focus moves to the
 * confirm button on open and is restored on close; Escape and backdrop dismiss.
 */
import { useId, useRef } from 'react';
import { useFocusTrap } from '@/lib/useFocusTrap';

interface ConfirmDialogProps {
  open: boolean;
  title: string;
  description: string;
  confirmLabel: string;
  /** Disable buttons + show a pending label while the action runs. */
  busy?: boolean;
  /** Label shown on the confirm button while `busy` (e.g. "Deleting…"). */
  busyLabel?: string;
  /**
   * A failure from the confirmed action, rendered INSIDE the dialog as a live
   * alert so the outcome is never silently discarded (e.g. a 403 on an
   * admin-gated delete). The caller keeps the dialog open on error and passes
   * the mapped human message here; Cancel stays available.
   */
  error?: string | null;
  onConfirm: () => void;
  onCancel: () => void;
}

export function ConfirmDialog({
  open,
  title,
  description,
  confirmLabel,
  busy = false,
  busyLabel = 'Removing…',
  error = null,
  onConfirm,
  onCancel,
}: ConfirmDialogProps) {
  const titleId = useId();
  const descId = useId();
  const dialogRef = useRef<HTMLDivElement>(null);
  const confirmRef = useRef<HTMLButtonElement>(null);

  // Focus the confirm button on open, trap Tab inside the dialog, restore focus
  // on close. Escape is gated on `busy` so it can't cancel mid-action.
  useFocusTrap(open, dialogRef, () => !busy && onCancel(), { initialFocus: confirmRef });

  if (!open) return null;

  return (
    <div
      ref={dialogRef}
      role="alertdialog"
      aria-modal="true"
      aria-labelledby={titleId}
      aria-describedby={descId}
      // Programmatically focusable so the focus-trap's zero-focusable fallback
      // (`container.focus()`) works while both buttons are disabled (`busy`).
      tabIndex={-1}
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
          {error ? (
            <p role="alert" className="rounded-md bg-danger/10 px-2 py-1.5 text-sm text-danger">
              {error}
            </p>
          ) : null}
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
            {busy ? busyLabel : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
