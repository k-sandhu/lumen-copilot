/**
 * SearchResultRow (#84) — one permission-trimmed ranked result. Built from the
 * trust-signal kit (`@/ui`): the matched snippet is `<mark>`-highlighted from the
 * server's `match_spans`, with the why-it-matched rationale, owner, a FreshnessPill
 * (amber when stale), and a PermissionPill. Every glance carries the trust signals
 * the mission requires — permission + freshness — without a click.
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
}

export function SearchResultRow({ result }: SearchResultRowProps) {
  const passage = toPassageRuns(result.snippet, result.match_spans);
  const fresh = freshnessLabel(result.last_indexed);
  const stale = isStale(result.last_indexed);
  const level = toPermissionLevel(result.permission);

  return (
    <article
      className="rounded-lg border border-border bg-surface p-4"
      aria-label={`Result: ${result.title}`}
    >
      <div className="flex items-start gap-3">
        <span className="select-none text-lg leading-none" aria-hidden="true">
          {sourceGlyph(result.source)}
        </span>
        <div className="min-w-0 flex-1">
          <header className="flex flex-wrap items-center gap-x-3 gap-y-1">
            <h3 className="min-w-0 truncate text-sm font-semibold text-foreground">
              {result.title}
            </h3>
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
