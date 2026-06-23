/**
 * SearchScreen (#84) — the search feature root. Composes the composer, the
 * optional cited direct answer, the ranked result rows, and the permission-trim
 * notice, and implements every async state (frontend/AGENTS.md "every state, not
 * just success"):
 *
 *   initial  → a prompt to search (no query submitted yet)
 *   loading  → skeleton rows while the search runs
 *   error    → an actionable message with retry (401/422 surfaced distinctly)
 *   empty    → "no results" for a submitted query that matched nothing
 *   success  → direct answer (if any) + trim notice + ranked rows
 *
 * The draft query and the submitted query are the only client-side state and live
 * here; the results are server state via `useSearch` (never mirrored into a store).
 * The body scrolls independently of the pinned composer (min-h-0 + ScrollArea).
 */
import { useMemo, useState } from 'react';
import { ApiError } from '@/api';
import type { SearchResult } from '@/api';
import { ScrollArea } from '@/components/ScrollArea';
import { Icon } from '@/ui';
import { useSearch } from '../model/queries';
import { SearchComposer } from './SearchComposer';
import { DirectAnswerBlock } from './DirectAnswerBlock';
import { SearchResultRow } from './SearchResultRow';
import { TrimNotice } from './TrimNotice';

/** Map a transport failure to a user-facing, actionable message. */
function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 401) return 'Your session expired. Sign in again to search.';
    if (error.status === 403) return 'You don’t have permission to run this search.';
    if (error.status === 422) return 'That query couldn’t be understood. Try rephrasing it.';
    return error.displayMessage || 'Search failed. Please try again.';
  }
  return 'Search failed. Please try again.';
}

export function SearchScreen() {
  const [draft, setDraft] = useState('');
  const [submitted, setSubmitted] = useState('');

  const query = useSearch({ q: submitted });
  const data = query.data;

  // Index results by id so the direct answer's citations resolve to their source.
  const resultsById = useMemo(() => {
    const map = new Map<string, SearchResult>();
    for (const r of data?.results ?? []) map.set(r.id, r);
    return map;
  }, [data]);

  const hasQuery = submitted.trim().length > 0;
  const isLoading = hasQuery && query.isLoading;
  const isError = hasQuery && query.isError;
  const isEmpty =
    hasQuery && query.isSuccess && (data?.results.length ?? 0) === 0 && !data?.direct_answer;

  return (
    <div className="flex h-full min-h-0 flex-col">
      {/* Pinned composer — stays put while results scroll below it. */}
      <div className="shrink-0 border-b border-border bg-surface p-4">
        <div className="mx-auto w-full max-w-3xl">
          <SearchComposer
            value={draft}
            onChange={setDraft}
            onSubmit={(q) => setSubmitted(q)}
            busy={isLoading || query.isFetching}
          />
        </div>
      </div>

      {/* Independently scrollable results pane. */}
      <ScrollArea viewportClassName="px-4 py-6">
        <div className="mx-auto w-full max-w-3xl space-y-4">
          {!hasQuery ? (
            <InitialState />
          ) : isLoading ? (
            <LoadingState />
          ) : isError ? (
            <ErrorState message={errorMessage(query.error)} onRetry={() => void query.refetch()} />
          ) : isEmpty ? (
            <EmptyState query={data?.query ?? submitted} />
          ) : data ? (
            <>
              {data.direct_answer ? (
                <DirectAnswerBlock answer={data.direct_answer} resultsById={resultsById} />
              ) : null}

              <TrimNotice hiddenCount={data.hidden_count} />

              <ul className="space-y-3" aria-label="Search results">
                {data.results.map((result) => (
                  <li key={result.id}>
                    <SearchResultRow result={result} />
                  </li>
                ))}
              </ul>
            </>
          ) : null}
        </div>
      </ScrollArea>
    </div>
  );
}

function InitialState() {
  return (
    <div className="flex flex-col items-center gap-2 py-16 text-center text-foreground-muted">
      <Icon name="search" aria-hidden="true" />
      <p className="text-sm">Search across your connected sources and uploaded documents.</p>
      <p className="text-xs">
        Results are permission-trimmed — you only ever see what you can access.
      </p>
    </div>
  );
}

function LoadingState() {
  return (
    <div aria-busy="true" aria-label="Searching" className="space-y-3">
      {[0, 1, 2].map((i) => (
        <div key={i} className="rounded-lg border border-border bg-surface p-4">
          <div className="lc-skeleton" style={{ width: '40%' }} />
          <div className="lc-skeleton" style={{ width: '100%', marginTop: 10 }} />
          <div className="lc-skeleton" style={{ width: '85%', marginTop: 8 }} />
        </div>
      ))}
    </div>
  );
}

function ErrorState({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <div
      role="alert"
      className="flex flex-col items-center gap-3 rounded-lg border border-danger/40 bg-danger/10 p-6 text-center"
    >
      <Icon name="alert-triangle" aria-hidden="true" />
      <p className="text-sm text-danger">{message}</p>
      <button
        type="button"
        onClick={onRetry}
        className="rounded-md border border-border bg-surface px-3 py-1.5 text-sm text-foreground hover:bg-surface-muted"
      >
        Try again
      </button>
    </div>
  );
}

function EmptyState({ query }: { query: string }) {
  return (
    <div className="flex flex-col items-center gap-2 py-16 text-center text-foreground-muted">
      <Icon name="search" aria-hidden="true" />
      <p className="text-sm">
        No results for <span className="font-medium text-foreground">“{query}”</span>.
      </p>
      <p className="text-xs">Try different words, or check that the source has been indexed.</p>
    </div>
  );
}
