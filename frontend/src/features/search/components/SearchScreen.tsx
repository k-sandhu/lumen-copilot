/**
 * SearchScreen (#84, polished #118) — the search feature root. Composes a page
 * head, the composer, a TWO-COLUMN body (filter sidebar left, answer + results
 * right), the optional cited direct answer, the ranked result rows, and the
 * permission-trim notice, and implements every async state (frontend/AGENTS.md
 * "every state, not just success"):
 *
 *   initial  → a prompt to search (no query submitted yet)
 *   loading  → skeleton rows while the search runs
 *   error    → an actionable message with retry (401/422 surfaced distinctly)
 *   empty    → "no results" for a submitted query that matched nothing
 *   success  → direct answer (if any) + trim notice + ranked rows
 *
 * Filters (#118) are backed by the REAL `/search` contract only: a collection
 * scope (`collection_id`), a source-kind scope (`source`, the frozen
 * `ResultSource` enum) AND a content-type scope (`type`) ALL drive the server
 * query. Routing every facet through the server keeps `results`, `direct_answer`
 * and `hidden_count` coherent: a visible direct answer always cites passages that
 * are present in `results` (spec 0004 INV-3) — a client-only type filter could
 * hide a cited row and leave the answer with dropped/literal citations, so it is
 * deliberately not used. No invented connectors.
 *
 * The draft query, the submitted query and the filter state are the only
 * client-side state and live here; the results are server state via `useSearch`
 * (never mirrored into a store). The results pane scrolls independently of the
 * pinned composer (min-h-0 + ScrollArea).
 */
import { useMemo, useState } from 'react';
import { ApiError } from '@/api';
import type { SearchResult } from '@/api';
import { ScrollArea } from '@/components/ScrollArea';
import { Icon } from '@/ui';
import { useCreateSavedSearch, useSearch, useSearchCollections } from '../model/queries';
import { SearchTypeahead } from './SearchTypeahead';
import { DirectAnswerBlock } from './DirectAnswerBlock';
import { ResultPreviewDrawer } from './ResultPreviewDrawer';
import { SearchResultRow } from './SearchResultRow';
import { SearchFilters, type SearchFilterState } from './SearchFilters';
import { TrimNotice } from './TrimNotice';

/** The document a clicked result opened (#375), or null while none is open. */
interface OpenPreview {
  documentId: string;
  title: string;
}

/** Map a transport failure to a user-facing, actionable message. */
function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 401) return 'Your session expired. Sign in again to search.';
    if (error.status === 403) return 'You don’t have permission to run this search.';
    if (error.status === 503 && error.problem?.code === 'llm_unconfigured') {
      return 'Search is temporarily unavailable — the answer service isn’t configured yet. Try again shortly.';
    }
    if (error.status === 422) return 'That query couldn’t be understood. Try rephrasing it.';
    return error.displayMessage || 'Search failed. Please try again.';
  }
  return 'Search failed. Please try again.';
}

export function SearchScreen() {
  const [draft, setDraft] = useState('');
  const [submitted, setSubmitted] = useState('');
  const [filters, setFilters] = useState<SearchFilterState>({});
  const [preview, setPreview] = useState<OpenPreview | null>(null);

  // Collection, source AND content-type are ALL server params: routing every
  // facet through `/search` keeps results + direct_answer + hidden_count coherent
  // (the server re-derives the answer over the filtered, permission-trimmed set),
  // so a visible answer's citations always resolve to a row in `results` (INV-3).
  const query = useSearch({
    q: submitted,
    collection_id: filters.collectionId,
    source: filters.source,
    type: filters.type,
  });
  const collections = useSearchCollections();
  const data = query.data;

  const results = useMemo(() => data?.results ?? [], [data]);

  // Index the server-returned (already type-filtered) results by id so each of
  // the direct answer's citations resolves to a source the user can see.
  const resultsById = useMemo(() => {
    const map = new Map<string, SearchResult>();
    for (const r of results) map.set(r.id, r);
    return map;
  }, [results]);

  const hasQuery = submitted.trim().length > 0;
  const hasFilter = Boolean(filters.collectionId || filters.source || filters.type);
  const isLoading = hasQuery && query.isLoading;
  const isError = hasQuery && query.isError;
  const isEmpty =
    hasQuery && query.isSuccess && (data?.results.length ?? 0) === 0 && !data?.direct_answer;

  return (
    <div className="flex h-full min-h-0 flex-col">
      {/* Page head + pinned composer — stay put while results scroll below. */}
      <div className="shrink-0 border-b border-border bg-surface px-6 pb-4 pt-5">
        <div className="mx-auto w-full max-w-5xl">
          <header className="mb-4">
            <h1 className="text-xl font-semibold text-foreground">Search</h1>
            <p className="mt-1 text-sm text-foreground-muted">
              One permissioned box across your connected sources and uploaded documents.
            </p>
          </header>
          <SearchTypeahead
            value={draft}
            onChange={setDraft}
            onRunQuery={(q) => {
              setDraft(q);
              setSubmitted(q);
            }}
            onApplySaved={(s) => {
              setDraft(s.query);
              setFilters({
                ...(s.collection_id ? { collectionId: s.collection_id } : {}),
                ...(s.source ? { source: s.source } : {}),
                ...(s.type ? { type: s.type } : {}),
              });
              setSubmitted(s.query);
            }}
            busy={isLoading || query.isFetching}
          />
        </div>
      </div>

      {/* Independently scrollable results pane. */}
      <ScrollArea viewportClassName="px-6 py-6">
        <div className="mx-auto w-full max-w-5xl">
          {!hasQuery ? (
            <InitialState />
          ) : isLoading ? (
            <LoadingState />
          ) : isError ? (
            <ErrorState message={errorMessage(query.error)} onRetry={() => void query.refetch()} />
          ) : isEmpty && !hasFilter ? (
            // Truly nothing matched (no active scope) — a plain empty state.
            <EmptyState query={data?.query ?? submitted} />
          ) : data ? (
            <div className="flex flex-col gap-6 md:flex-row">
              {/* LEFT: filter sidebar — backed by the real contract only. Kept
                  visible even on a filtered-empty result so the active scope is
                  always visible and clearable. */}
              <aside className="shrink-0 md:w-60">
                <div className="rounded-lg border border-border bg-surface p-4 md:sticky md:top-0">
                  <SearchFilters
                    state={filters}
                    onChange={setFilters}
                    collections={collections.data?.items ?? []}
                    collectionsLoading={collections.isLoading}
                    results={results}
                  />
                </div>
              </aside>

              {/* RIGHT: direct answer + ranked results. */}
              <div className="min-w-0 flex-1 space-y-4">
                {results.length === 0 ? (
                  // The active scope filtered every row away. Keep the sidebar
                  // (above) so the user can see and clear the scope, and offer a
                  // one-click reset here.
                  <FilteredEmptyState
                    query={data.query}
                    onClear={() => setFilters({})}
                  />
                ) : (
                  <>
                    {data.direct_answer ? (
                      <DirectAnswerBlock answer={data.direct_answer} resultsById={resultsById} />
                    ) : null}

                    <ResultsToolbar
                      count={results.length}
                      filtered={hasFilter}
                      query={data.query}
                      filters={filters}
                    />

                    <TrimNotice hiddenCount={data.hidden_count} />

                    <ul className="space-y-3" aria-label="Search results">
                      {results.map((result) => {
                        // Only a result that resolves to a document is openable
                        // (#375); rows without one stay non-interactive.
                        const documentId = result.document_id;
                        return (
                          <li key={result.id}>
                            <SearchResultRow
                              result={result}
                              {...(documentId
                                ? {
                                    onOpen: () =>
                                      setPreview({ documentId, title: result.title }),
                                  }
                                : {})}
                            />
                          </li>
                        );
                      })}
                    </ul>
                  </>
                )}
              </div>
            </div>
          ) : null}
        </div>
      </ScrollArea>

      {/* The document a clicked result opened (#375). */}
      {preview ? (
        <ResultPreviewDrawer
          documentId={preview.documentId}
          title={preview.title}
          onClose={() => setPreview(null)}
        />
      ) : null}
    </div>
  );
}

function ResultsToolbar({
  count,
  filtered,
  query,
  filters,
}: {
  count: number;
  filtered: boolean;
  query: string;
  filters: SearchFilterState;
}) {
  return (
    <div className="flex items-center justify-between gap-3">
      <p className="text-sm text-foreground-muted">
        <b className="text-foreground">{count}</b> {count === 1 ? 'result' : 'results'} ·
        permission-trimmed
        {filtered ? ' · filtered' : ''}
      </p>
      <SaveSearchControl query={query} filters={filters} />
    </div>
  );
}

/**
 * "Save search" — names the current query + filters into a saved search. Inline
 * (a button that expands to a small name field), so the user never leaves the
 * results. Applying a saved search later re-runs the same /search (the typeahead).
 */
function SaveSearchControl({ query, filters }: { query: string; filters: SearchFilterState }) {
  const create = useCreateSavedSearch();
  const [open, setOpen] = useState(false);
  const [name, setName] = useState('');

  if (!query.trim()) return null;

  function submit(e: React.FormEvent) {
    e.preventDefault();
    const n = name.trim();
    if (!n) return;
    create.mutate(
      {
        name: n,
        query,
        ...(filters.collectionId ? { collection_id: filters.collectionId } : {}),
        ...(filters.source ? { source: filters.source } : {}),
        ...(filters.type ? { type: filters.type } : {}),
      },
      {
        onSuccess: () => {
          setOpen(false);
          setName('');
        },
      },
    );
  }

  if (!open) {
    return (
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="inline-flex shrink-0 items-center gap-1.5 rounded-md border border-border px-2.5 py-1 text-xs font-medium text-foreground-muted hover:bg-surface-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
      >
        <Icon name="list" className="h-3.5 w-3.5" />
        Save search
      </button>
    );
  }

  return (
    <form onSubmit={submit} className="flex shrink-0 items-center gap-2">
      <input
        autoFocus
        value={name}
        onChange={(e) => setName(e.target.value)}
        aria-label="Saved search name"
        placeholder="Name this search"
        maxLength={200}
        className="w-40 rounded-md border border-border bg-surface px-2 py-1 text-xs text-foreground focus:outline-none focus:ring-2 focus:ring-accent"
      />
      <button
        type="submit"
        disabled={create.isPending || name.trim().length === 0}
        className="rounded-md bg-accent px-2.5 py-1 text-xs font-medium text-white hover:bg-accent/90 disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
      >
        {create.isPending ? 'Saving…' : 'Save'}
      </button>
      <button
        type="button"
        onClick={() => {
          setOpen(false);
          setName('');
        }}
        className="rounded-md px-2 py-1 text-xs text-foreground-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
      >
        Cancel
      </button>
      {create.isError ? (
        <span role="alert" className="text-xs text-danger">
          Couldn’t save.
        </span>
      ) : null}
    </form>
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

/**
 * When the active scope filters (collection / source / type) leave no results,
 * keep the sidebar (rendered alongside) and offer a one-click reset of the scope
 * so the user is never stranded with an empty pane and an invisible filter.
 */
function FilteredEmptyState({ query, onClear }: { query: string; onClear: () => void }) {
  return (
    <div
      role="status"
      className="rounded-lg border border-border bg-surface p-6 text-center text-sm text-foreground-muted"
    >
      <p>
        No results for <span className="font-medium text-foreground">“{query}”</span> under the
        active filters.
      </p>
      <button
        type="button"
        onClick={onClear}
        className="mt-2 rounded-md border border-border bg-surface px-3 py-1.5 text-foreground hover:bg-surface-muted"
      >
        Clear filters
      </button>
    </div>
  );
}
