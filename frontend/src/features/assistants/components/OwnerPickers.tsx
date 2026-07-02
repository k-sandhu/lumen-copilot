/**
 * OwnerPickers (#212, AC-1 / E6-8) — the accountable owner + backup owner
 * selects, populated from the tenant roster (GET /admin/members). Publishing an
 * assistant REQUIRES both an owner and a DISTINCT backup owner (ADR-0011 §4); the
 * editor disables Publish until both are set, and the server rejects a publish
 * without a backup as 422 (INV-8).
 *
 * The roster is admin-only per the contract: a non-admin caller receives 403
 * (INV-5). We surface that honestly ("roster unavailable — ask an admin") rather
 * than blanking the picker, and keep any already-set owner/backup selectable so a
 * draft's existing choices aren't lost.
 *
 * A11y: native labelled <select>s; the backup select excludes the chosen owner so
 * the "distinct" rule can't be violated from the UI.
 */
import { useId } from 'react';
import { ApiError } from '@/api';
import type { Member } from '@/api';

interface OwnerPickersProps {
  owner: string | null;
  backupOwner: string | null;
  onOwnerChange: (id: string | null) => void;
  onBackupChange: (id: string | null) => void;
  members: Member[] | undefined;
  isPending: boolean;
  isError: boolean;
  error: unknown;
  onRetry: () => void;
  disabled?: boolean;
}

export function OwnerPickers({
  owner,
  backupOwner,
  onOwnerChange,
  onBackupChange,
  members,
  isPending,
  isError,
  error,
  onRetry,
  disabled = false,
}: OwnerPickersProps) {
  const ownerId = useId();
  const backupId = useId();

  if (isPending) {
    return (
      <div role="status" aria-label="Loading members" className="space-y-2">
        <div className="lc-skeleton" style={{ width: '60%' }} aria-hidden="true" />
        <div className="lc-skeleton" style={{ width: '60%' }} aria-hidden="true" />
      </div>
    );
  }

  if (isError) {
    const forbidden = error instanceof ApiError && error.status === 403;
    const unauthorized = error instanceof ApiError && error.status === 401;
    return (
      <div role="alert" className="rounded-md border border-danger/40 bg-danger/10 p-3 text-xs">
        <p className="text-danger">
          {forbidden
            ? 'The member roster is admin-only — ask an admin to set the owner and backup owner.'
            : unauthorized
              ? 'Your session expired — sign in again.'
              : 'Couldn’t load the member roster.'}
        </p>
        {!forbidden && !unauthorized ? (
          <button
            type="button"
            onClick={onRetry}
            className="mt-1.5 rounded-md border border-border bg-surface px-2 py-1 text-xs hover:bg-surface-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
          >
            Retry
          </button>
        ) : null}
      </div>
    );
  }

  const roster = members ?? [];

  return (
    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
      <div className="min-w-0">
        <label htmlFor={ownerId} className="mb-1 block text-sm font-medium">
          Owner
        </label>
        <select
          id={ownerId}
          value={owner ?? ''}
          disabled={disabled}
          onChange={(e) => onOwnerChange(e.target.value || null)}
          className="w-full rounded-md border border-border bg-surface px-3 py-2 text-sm outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-60"
        >
          <option value="">Select an owner…</option>
          {roster.map((m) => (
            <option key={m.id} value={m.id}>
              {m.email}
            </option>
          ))}
        </select>
      </div>

      <div className="min-w-0">
        <label htmlFor={backupId} className="mb-1 block text-sm font-medium">
          Backup owner
        </label>
        <select
          id={backupId}
          value={backupOwner ?? ''}
          disabled={disabled}
          onChange={(e) => onBackupChange(e.target.value || null)}
          className="w-full rounded-md border border-border bg-surface px-3 py-2 text-sm outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-60"
        >
          <option value="">Select a backup…</option>
          {roster
            // Distinct-from-owner (ADR-0011 §4): the owner can't also be the backup.
            .filter((m) => m.id !== owner)
            .map((m) => (
              <option key={m.id} value={m.id}>
                {m.email}
              </option>
            ))}
        </select>
        <p className="mt-1 text-xs text-foreground-muted">
          Required before publishing — and must differ from the owner.
        </p>
      </div>
    </div>
  );
}
