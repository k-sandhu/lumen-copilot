/**
 * PanelState — the shared loading / empty / error scaffolding every admin panel
 * reuses, so no async surface ever renders a blank pane (frontend/AGENTS.md: "every
 * state, not just success"). Errors are actionable (a Retry button), never dead
 * ends; the admin governance reads can 403 (non-admin, INV-5) or 401 (expired
 * token, INV-4), and those land here as a typed message + retry.
 */
import type { ReactNode } from 'react';
import { ApiError } from '@/api';

/** Rows of shimmer placeholders while a panel's data is in flight. */
export function PanelLoading({ rows = 3, label }: { rows?: number; label: string }) {
  return (
    <div role="status" aria-live="polite" aria-label={`Loading ${label}`} className="space-y-2 p-4">
      <span className="sr-only">Loading {label}…</span>
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} aria-hidden="true" className="h-8 animate-pulse rounded-md bg-surface-muted" />
      ))}
    </div>
  );
}

/** Neutral empty state — the read succeeded but returned nothing. */
export function PanelEmpty({ message }: { message: string }) {
  return (
    <p className="p-6 text-center text-sm text-foreground-muted" role="note">
      {message}
    </p>
  );
}

/** Best human-readable message for an error, preferring the RFC-9457 Problem. */
function describeError(error: unknown): { message: string; status?: number } {
  if (error instanceof ApiError) {
    if (error.status === 403) {
      return { message: 'You need the admin role to view this.', status: 403 };
    }
    if (error.status === 401) {
      return { message: 'Your session has expired. Sign in again.', status: 401 };
    }
    return { message: error.displayMessage, status: error.status };
  }
  if (error instanceof Error) return { message: error.message };
  return { message: 'Something went wrong loading this panel.' };
}

/** Actionable error state — a typed message plus a Retry that refetches. */
export function PanelError({ error, onRetry }: { error: unknown; onRetry: () => void }) {
  const { message } = describeError(error);
  return (
    <div role="alert" className="m-4 rounded-md border border-danger/40 bg-danger/5 p-4 text-sm">
      <p className="font-medium text-foreground">Couldn’t load this panel</p>
      <p className="mt-1 text-foreground-muted">{message}</p>
      <button
        type="button"
        onClick={onRetry}
        className="mt-3 rounded-md border border-border px-3 py-1 text-sm hover:bg-surface-muted"
      >
        Retry
      </button>
    </div>
  );
}

/**
 * Renders one of loading/error/empty/success for a panel. `isEmpty` distinguishes
 * a successful-but-empty read from real content so the empty state shows instead
 * of an awkward blank table.
 */
export function PanelBody({
  label,
  isLoading,
  error,
  isEmpty,
  emptyMessage,
  onRetry,
  loadingRows,
  children,
}: {
  label: string;
  isLoading: boolean;
  error: unknown;
  isEmpty: boolean;
  emptyMessage: string;
  onRetry: () => void;
  loadingRows?: number;
  children: ReactNode;
}) {
  if (isLoading) return <PanelLoading label={label} rows={loadingRows} />;
  if (error) return <PanelError error={error} onRetry={onRetry} />;
  if (isEmpty) return <PanelEmpty message={emptyMessage} />;
  return <>{children}</>;
}
