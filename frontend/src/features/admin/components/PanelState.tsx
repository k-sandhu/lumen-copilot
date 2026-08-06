/**
 * PanelState — the shared loading / empty / error scaffolding every admin panel
 * reuses, so no async surface ever renders a blank pane (frontend/AGENTS.md: "every
 * state, not just success"). The admin governance reads can 403 (non-admin, INV-5)
 * or 401 (expired token, INV-4), and those land here as a typed message.
 *
 * Errors are actionable (a Retry button) by default. A panel whose failure a
 * retry could never fix — a role-gated read that 403s — opts into
 * `deadEndStatuses` so it states the access truth instead of offering a button
 * that reloads the same refusal.
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
export function PanelError({
  error,
  onRetry,
  deadEndStatuses = [],
}: {
  error: unknown;
  onRetry: () => void;
  /**
   * Statuses a Retry could never fix, so the button is suppressed and the copy
   * becomes an honest access explanation instead (frontend/AGENTS.md: errors are
   * actionable OR an honest dead end — never a retry that cannot succeed).
   *
   * Defaults to `[]`, which is every existing panel's behaviour unchanged. A
   * role-gated admin read should opt into `[401, 403]`; the correct set differs
   * by surface (an owner-scoped read uses 401/404, where 404 is existence
   * non-disclosure, INV-1), which is why it is the caller's decision.
   */
  deadEndStatuses?: number[];
}) {
  const { message, status } = describeError(error);
  const deadEnd = status !== undefined && deadEndStatuses.includes(status);
  // A dead end is not always an access problem: a caller that opts 404 in means
  // the thing is gone, and "you don't have access" would misdescribe it. Only
  // reachable for opt-in callers, so no existing panel's copy changes.
  const headline = !deadEnd
    ? 'Couldn’t load this panel'
    : status === 404
      ? 'This is no longer available'
      : 'You don’t have access to this panel';
  return (
    <div role="alert" className="m-4 rounded-md border border-danger/40 bg-danger/5 p-4 text-sm">
      <p className="font-medium text-foreground">{headline}</p>
      <p className="mt-1 text-foreground-muted">{message}</p>
      {deadEnd ? null : (
        <button
          type="button"
          onClick={onRetry}
          className="mt-3 rounded-md border border-border px-3 py-1 text-sm hover:bg-surface-muted"
        >
          Retry
        </button>
      )}
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
  deadEndStatuses,
  children,
}: {
  label: string;
  isLoading: boolean;
  error: unknown;
  isEmpty: boolean;
  emptyMessage: string;
  onRetry: () => void;
  loadingRows?: number;
  /** Forwarded to `PanelError` — see its docstring. Default: everything retries. */
  deadEndStatuses?: number[];
  children: ReactNode;
}) {
  if (isLoading) return <PanelLoading label={label} rows={loadingRows} />;
  if (error) return <PanelError error={error} onRetry={onRetry} deadEndStatuses={deadEndStatuses} />;
  if (isEmpty) return <PanelEmpty message={emptyMessage} />;
  return <>{children}</>;
}
