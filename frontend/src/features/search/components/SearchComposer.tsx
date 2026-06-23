/**
 * SearchComposer (#84) — the type-to-query input. A real <form> so Enter and the
 * submit button both fire `onSubmit`; submitting an empty/whitespace query is a
 * no-op (the screen stays in its initial empty state rather than firing the
 * 422-on-empty path). The draft text is local UI state owned by the parent so the
 * composer stays presentational.
 *
 * Accessible: a labelled search box (role=searchbox via type="search"), a busy
 * indication on the submit control, and a visible clear affordance.
 */
import { useId } from 'react';
import { Icon } from '@/ui';
import { cn } from '@/lib/cn';

interface SearchComposerProps {
  /** Current draft text (controlled by the parent). */
  value: string;
  onChange: (next: string) => void;
  /** Fired with the trimmed query on submit; the parent runs the search. */
  onSubmit: (query: string) => void;
  /** A search is in flight (drives the busy state on submit). */
  busy?: boolean;
  className?: string;
}

export function SearchComposer({
  value,
  onChange,
  onSubmit,
  busy = false,
  className,
}: SearchComposerProps) {
  const inputId = useId();
  const trimmed = value.trim();

  return (
    <form
      role="search"
      className={cn('flex items-center gap-2', className)}
      onSubmit={(e) => {
        e.preventDefault();
        if (trimmed.length > 0) onSubmit(trimmed);
      }}
    >
      <label htmlFor={inputId} className="sr-only">
        Search your sources
      </label>
      <div className="relative flex min-w-0 flex-1 items-center">
        <span
          className="pointer-events-none absolute left-3 text-foreground-muted"
          aria-hidden="true"
        >
          <Icon name="search" />
        </span>
        <input
          id={inputId}
          type="search"
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder="Search across your permitted sources…"
          autoComplete="off"
          aria-label="Search your sources"
          className="w-full rounded-md border border-border bg-surface py-2 pl-10 pr-3 text-sm text-foreground placeholder:text-foreground-muted focus:outline-none focus:ring-2 focus:ring-accent"
        />
      </div>
      <button
        type="submit"
        disabled={busy || trimmed.length === 0}
        aria-busy={busy}
        className="shrink-0 rounded-md bg-accent px-4 py-2 text-sm font-medium text-white hover:bg-accent/90 disabled:cursor-not-allowed disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
      >
        {busy ? 'Searching…' : 'Search'}
      </button>
    </form>
  );
}
