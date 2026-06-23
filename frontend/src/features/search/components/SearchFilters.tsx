/**
 * SearchFilters (#118) — the left filter sidebar of the two-column search layout.
 *
 * SCOPE GUARD (issue #118): every filter here is backed by the REAL `/search`
 * contract + real data — never an invented connector. The three facets are:
 *
 *   • Collection — the `collection_id` query param (from `GET /collections`).
 *   • Source     — the FROZEN `ResultSource` enum (upload | chat | connector),
 *                  i.e. uploaded documents / chat messages / connected sources.
 *                  No Slack/Jira/Tickets/Code/People — those aren't in the contract.
 *   • Content type — derived CLIENT-SIDE from the `type` strings the server
 *                  actually returned (never a hardcoded list the backend may not
 *                  serve), so the counts are honest.
 *
 * Collection + source drive the server query (they change the result set + counts);
 * the content-type facet narrows the returned rows client-side. Presentational —
 * all state is owned by the parent and all wire→facet mapping lives in
 * `model/presentation.ts`.
 */
import type { Collection, ResultSource, SearchResult } from '@/api';
import { Icon } from '@/ui';
import { cn } from '@/lib/cn';
import { sourceFacets, sourceLabel, typeFacets } from '../model/presentation';

export interface SearchFilterState {
  /** Scope to one collection, or undefined for all permitted collections. */
  collectionId?: string;
  /** Scope to one source kind, or undefined for all source kinds. */
  source?: ResultSource;
  /** Client-side narrow to one content type, or undefined for all types. */
  type?: string;
}

interface SearchFiltersProps {
  state: SearchFilterState;
  onChange: (next: SearchFilterState) => void;
  /** The caller's collections (for the collection scope). */
  collections: Collection[];
  /** The current (permitted) results, for honest data-derived facet counts. */
  results: SearchResult[];
  /** Collections are still loading (skeleton the collection group). */
  collectionsLoading?: boolean;
}

export function SearchFilters({
  state,
  onChange,
  collections,
  results,
  collectionsLoading = false,
}: SearchFiltersProps) {
  const sources = sourceFacets(results);
  const types = typeFacets(results);

  return (
    <nav aria-label="Search filters" className="space-y-6 text-sm">
      {/* --- Collection scope (server param: collection_id) --- */}
      <FilterGroup title="Collection">
        <FilterRow
          label="All collections"
          active={state.collectionId === undefined}
          onClick={() => onChange({ ...state, collectionId: undefined })}
        />
        {collectionsLoading ? (
          <div aria-hidden="true" className="space-y-1.5 px-1 py-1">
            <div className="lc-skeleton" style={{ width: '80%' }} />
            <div className="lc-skeleton" style={{ width: '65%' }} />
          </div>
        ) : (
          collections.map((c) => (
            <FilterRow
              key={c.id}
              label={c.name}
              count={c.document_count}
              active={state.collectionId === c.id}
              onClick={() =>
                onChange({
                  ...state,
                  collectionId: state.collectionId === c.id ? undefined : c.id,
                })
              }
            />
          ))
        )}
      </FilterGroup>

      {/* --- Source kind (server param: source — the frozen ResultSource enum) --- */}
      <FilterGroup title="Source">
        <FilterRow
          label="All sources"
          active={state.source === undefined}
          onClick={() => onChange({ ...state, source: undefined })}
        />
        {sources.length === 0 ? (
          <p className="px-1 text-xs text-foreground-muted">No sources in these results yet.</p>
        ) : (
          sources.map(({ source, count }) => (
            <FilterRow
              key={source}
              label={sourceLabel(source)}
              count={count}
              active={state.source === source}
              onClick={() =>
                onChange({
                  ...state,
                  source: state.source === source ? undefined : source,
                })
              }
            />
          ))
        )}
      </FilterGroup>

      {/* --- Content type (client-side facet, derived from the data) --- */}
      {types.length > 0 ? (
        <FilterGroup title="Content type">
          <FilterRow
            label="Any type"
            active={state.type === undefined}
            onClick={() => onChange({ ...state, type: undefined })}
          />
          {types.map(({ type, count }) => (
            <FilterRow
              key={type}
              label={type}
              count={count}
              capitalize
              active={state.type === type}
              onClick={() =>
                onChange({
                  ...state,
                  type: state.type === type ? undefined : type,
                })
              }
            />
          ))}
        </FilterGroup>
      ) : null}
    </nav>
  );
}

function FilterGroup({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-foreground-muted">
        {title}
      </div>
      <div className="space-y-0.5">{children}</div>
    </div>
  );
}

function FilterRow({
  label,
  count,
  active,
  onClick,
  capitalize = false,
}: {
  label: string;
  count?: number;
  active: boolean;
  onClick: () => void;
  capitalize?: boolean;
}) {
  return (
    <button
      type="button"
      role="checkbox"
      aria-checked={active}
      onClick={onClick}
      className={cn(
        'flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-sm transition-colors hover:bg-surface-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
        active ? 'text-foreground' : 'text-foreground-muted',
      )}
    >
      <span
        aria-hidden="true"
        className={cn(
          'flex h-4 w-4 shrink-0 items-center justify-center rounded border',
          active ? 'border-accent bg-accent text-white' : 'border-border bg-surface',
        )}
      >
        {active ? <Icon name="check" /> : null}
      </span>
      <span className={cn('min-w-0 flex-1 truncate', capitalize && 'capitalize')}>{label}</span>
      {count !== undefined ? (
        <span className="shrink-0 text-xs tabular-nums text-foreground-muted">{count}</span>
      ) : null}
    </button>
  );
}
