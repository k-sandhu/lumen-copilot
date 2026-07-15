/**
 * SearchResultRow (#84) — one permission-trimmed ranked result. Built from the
 * trust-signal kit (`@/ui`): the matched snippet is `<mark>`-highlighted from the
 * server's `match_spans`, with the why-it-matched rationale, owner, a FreshnessPill
 * (amber when stale), and a PermissionPill. Every glance carries the trust signals
 * the mission requires — permission + freshness — without a click.
 *
 * Click-through (#375): when the caller supplies `onOpen` (i.e. the result
 * resolves to a document), the title renders as an "Open …" button so the found
 * document is one interaction away. The snippet stays plain text (selectable);
 * a row with no destination (no `document_id`) stays non-interactive.
 *
 * Presentational: all wire→prop mapping is done in `model/presentation.ts`, so
 * this component just composes kit primitives.
 */
import type { SearchResult } from '@/api';
import { FreshnessPill, Icon, PermissionPill } from '@/ui';
import {
  freshnessLabel,
  isStale,
  permissionLabel,
  sourceGlyph,
  toPassageRuns,
  toPermissionLevel,
} from '../model/presentation';

interface SearchResultRowProps {
  result: SearchResult;
  /** Opens the result's document (present only when it resolves to one, #375). */
  onOpen?: () => void;
}

export function SearchResultRow({ result, onOpen }: SearchResultRowProps) {
  const passage = toPassageRuns(result.snippet, result.match_spans);
  const fresh = freshnessLabel(result.last_indexed);
  const stale = isStale(result.last_indexed);
  const level = toPermissionLevel(result.permission);

  return (
    <article
      className={`rounded-lg border border-border bg-surface p-4 transition-colors ${
        onOpen ? 'hover:border-accent/50 focus-within:border-accent/50' : ''
      }`}
      aria-label={`Result: ${result.title}`}
    >
      <div className="flex items-start gap-3">
        <span className="select-none text-lg leading-none" aria-hidden="true">
          {sourceGlyph(result.source)}
        </span>
        <div className="min-w-0 flex-1">
          <header className="flex flex-wrap items-center gap-x-3 gap-y-1">
            {onOpen ? (
              <h3 className="min-w-0 truncate text-sm font-semibold text-foreground">
                <button
                  type="button"
                  onClick={onOpen}
                  aria-label={`Open ${result.title}`}
                  className="inline-flex max-w-full items-center gap-1.5 truncate rounded-sm text-left hover:text-accent hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
                >
                  <span className="truncate">{result.title}</span>
                  <Icon name="arrow-up-right" aria-hidden="true" className="h-3.5 w-3.5 shrink-0" />
                </button>
              </h3>
            ) : (
              <h3 className="min-w-0 truncate text-sm font-semibold text-foreground">
                {result.title}
              </h3>
            )}
            <span className="rounded-full bg-surface-muted px-2 py-0.5 text-xs text-foreground-muted">
              {result.type}
            </span>
          </header>

          <p className="mt-2 text-sm leading-relaxed text-foreground-muted">
            {passage.runs.map((run, i) =>
              run.highlight ? (
                <mark key={i} className="rounded bg-accent/20 text-foreground">
                  {run.text}
                </mark>
              ) : (
                <span key={i}>{run.text}</span>
              ),
            )}
          </p>

          {/* Why it matched — the rationale that makes ranking legible. */}
          <p className="mt-2 inline-flex items-center gap-1.5 text-xs text-foreground-muted">
            <Icon name="search" aria-hidden="true" />
            <span>
              <span className="font-medium">Why it matched:</span> {result.why_matched}
            </span>
          </p>

          <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-2">
            {result.owner ? (
              <span className="inline-flex items-center gap-1.5 text-xs text-foreground-muted">
                <Icon name="user" aria-hidden="true" />
                {result.owner}
              </span>
            ) : null}
            <FreshnessPill label={fresh} stale={stale} title={result.last_indexed} />
            <PermissionPill level={level} label={permissionLabel(result.permission)} />
          </div>
        </div>
      </div>
    </article>
  );
}
