/**
 * Shared async-state views for the schedules + runs slice (#237) — loading
 * skeletons, an actionable error (retry, with honest 401/404 messaging), and an
 * empty placeholder. Every async surface in the slice reuses these so no pane is
 * ever blank (frontend/AGENTS.md "every state, not just success").
 */
import { ApiError } from '@/api';
import { Icon } from '@/ui';

/** N pulsing skeleton rows for a loading list. */
export function SkeletonRows({ count = 5, label }: { count?: number; label: string }) {
  return (
    <div role="status" aria-live="polite" aria-busy="true" className="space-y-2">
      <span className="sr-only">{label}</span>
      {Array.from({ length: count }, (_, i) => (
        <div
          key={i}
          className="h-16 animate-pulse rounded-md bg-surface-muted"
          aria-hidden="true"
        />
      ))}
    </div>
  );
}

/** A centered empty placeholder — headline + guidance, never a blank pane. */
export function EmptyState({ title, message }: { title: string; message: string }) {
  return (
    <div className="px-2 py-16 text-center">
      <p className="text-sm font-medium">{title}</p>
      <p className="mt-1 text-sm text-foreground-muted">{message}</p>
    </div>
  );
}

/**
 * An actionable error. A 401 (INV-4) is a re-auth dead-end and a 404 (INV-1/2) is
 * existence non-disclosure — both are messaged honestly and NOT offered a
 * pointless retry; everything else gets a retry button.
 */
export function ErrorState({
  error,
  onRetry,
  busy,
  title,
}: {
  error: unknown;
  onRetry: () => void;
  busy: boolean;
  title: string;
}) {
  const status = error instanceof ApiError ? error.status : 0;
  const deadEnd = status === 401 || status === 404;
  const message =
    status === 401
      ? 'Your session expired. Sign in again to continue.'
      : status === 404
        ? 'This no longer exists, or you don’t have access to it.'
        : error instanceof ApiError
          ? error.displayMessage || 'Something went wrong.'
          : 'Something went wrong.';

  return (
    <div role="alert" className="space-y-2 px-2 py-8 text-sm">
      <p className="flex items-center gap-2 font-medium text-danger">
        <Icon name="alert-triangle" />
        {title}
      </p>
      <p className="text-foreground-muted">{message}</p>
      {!deadEnd ? (
        <button
          type="button"
          onClick={onRetry}
          disabled={busy}
          className="rounded-md border border-border bg-surface px-3 py-1.5 hover:bg-surface-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-60"
        >
          {busy ? 'Retrying…' : 'Retry'}
        </button>
      ) : null}
    </div>
  );
}

/** Shared cursor pagination control (prev/next over an opaque cursor stack). */
export function Pagination({
  canPrev,
  canNext,
  onPrev,
  onNext,
  page,
  label,
}: {
  canPrev: boolean;
  canNext: boolean;
  onPrev: () => void;
  onNext: () => void;
  page: number;
  label: string;
}) {
  return (
    <nav
      aria-label={label}
      className="flex shrink-0 items-center justify-between border-t border-border px-4 py-2 text-sm"
    >
      <span className="text-foreground-muted">Page {page}</span>
      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={onPrev}
          disabled={!canPrev}
          className="rounded-md border border-border px-3 py-1 hover:bg-surface-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-50"
        >
          Previous
        </button>
        <button
          type="button"
          onClick={onNext}
          disabled={!canNext}
          className="rounded-md border border-border px-3 py-1 hover:bg-surface-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-50"
        >
          Next
        </button>
      </div>
    </nav>
  );
}
