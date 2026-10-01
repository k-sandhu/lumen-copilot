/**
 * DirectAnswerBlock (#84, polished #118) — the optional cited direct answer.
 * Present only when the permitted results support it; every claim is backed by a
 * citation that references a passage present in `results` (spec 0004 INV-3) — so an
 * answer can never cite something the caller could not retrieve.
 *
 * Wireframe fidelity (#118):
 *   • a "Cited" badge in the head and an "Evidence: N sources" badge,
 *   • permission ("Permission-checked") + freshness ("Freshest source …") meta pills
 *     derived from the cited results,
 *   • INLINE `[n]` citation markers WITHIN the answer text (not a separate chip row):
 *     each `[n]` the answer text carries becomes a clickable CitationChip in place.
 *     When the answer carries no inline markers we fall back to a trailing marker
 *     row so the answer is never left un-navigable.
 *
 * The answer text is rendered through the ONE sanitizing markdown pipeline for any
 * plain run (`MarkdownView`), never raw (frontend/AGENTS.md "Rendered, never raw").
 * Clicking a marker opens the matching SourceInspector with the exact cited passage
 * (highlighted), owner, freshness and permission. A citation whose `result_id` is
 * absent from `results` is dropped defensively — the UI refuses to render an
 * un-resolvable (un-verifiable) citation.
 */
import { Fragment, useMemo, useState } from 'react';
import type { DirectAnswer, SearchResult } from '@/api';
import { CitationChip, FreshnessPill, Icon, PermissionPill, SourceInspector } from '@/ui';
import { MarkdownView } from '@/lib/markdown';
import { validMediaTimeSpan } from '@/lib/mediaTime';
import {
  freshnessLabel,
  hasInlineCitations,
  isStale,
  segmentAnswer,
  toPassageRuns,
  toPermissionLevel,
} from '../model/presentation';

interface DirectAnswerBlockProps {
  answer: DirectAnswer;
  /** The page's results, indexed by id, so each citation resolves to its source. */
  resultsById: Map<string, SearchResult>;
  onOpenDocument?: (documentId: string, title: string, initialTimeMs?: number) => void;
}

interface ResolvedCitation {
  citation: DirectAnswer['citations'][number];
  result: SearchResult;
  ordinal: number;
  timeStartMs?: number;
}

export function DirectAnswerBlock({ answer, resultsById, onOpenDocument }: DirectAnswerBlockProps) {
  const [openIndex, setOpenIndex] = useState<number | null>(null);

  // Only citations that resolve to a present result are renderable — a citation
  // must trace to a retrievable passage (INV-3). Drop any that don't. The ordinal
  // stays stable across the full citations list so an inline `[n]` marker in the
  // answer text still points at the right (possibly dropped) source slot.
  const resolved = useMemo<ResolvedCitation[]>(
    () =>
      answer.citations
        .map((citation, i) => {
          const span = validMediaTimeSpan(citation.time_start_ms, citation.time_end_ms);
          return {
            citation,
            result: resultsById.get(citation.result_id),
            ordinal: i + 1,
            ...(span ? { timeStartMs: span.startMs } : {}),
          };
        })
        .filter((entry): entry is ResolvedCitation => Boolean(entry.result)),
    [answer.citations, resultsById],
  );

  const resolvedByOrdinal = useMemo(() => new Map(resolved.map((r) => [r.ordinal, r])), [resolved]);

  const toggle = (ordinal: number) => setOpenIndex((cur) => (cur === ordinal ? null : ordinal));

  const activate = (ordinal: number) => {
    toggle(ordinal);
    const resolvedCitation = resolvedByOrdinal.get(ordinal);
    const documentId = resolvedCitation?.result.document_id;
    if (resolvedCitation && documentId && resolvedCitation.timeStartMs !== undefined) {
      onOpenDocument?.(documentId, resolvedCitation.result.title, resolvedCitation.timeStartMs);
    }
  };

  const open = openIndex === null ? null : (resolvedByOrdinal.get(openIndex) ?? null);

  // Freshest cited source drives the "Freshest source …" meta pill.
  const freshest = useMemo(() => {
    let best: SearchResult | null = null;
    for (const { result } of resolved) {
      if (!best || Date.parse(result.last_indexed) > Date.parse(best.last_indexed)) best = result;
    }
    return best;
  }, [resolved]);

  // Whether any cited source is restricted (content-withheld) — surfaced honestly.
  const anyRestricted = resolved.some((r) => r.result.permission === 'restricted');

  const inline = hasInlineCitations(answer.text, answer.citations.length);

  return (
    <section
      aria-label="Direct answer"
      className="rounded-lg border border-accent/40 bg-accent/5 p-4"
    >
      <div className="mb-2 flex items-center gap-2">
        <Icon name="sparkles" aria-hidden="true" className="text-accent" />
        <h2 className="text-xs font-semibold uppercase tracking-wide text-accent">Direct answer</h2>
        {resolved.length > 0 ? (
          <span className="lc-badge lc-badge--info ml-auto">Cited</span>
        ) : null}
      </div>

      {/* The answer body — inline `[n]` markers become CitationChips in place. */}
      <div className="text-sm leading-relaxed text-foreground">
        {inline ? (
          <InlineAnswer
            text={answer.text}
            citationCount={answer.citations.length}
            resolvedByOrdinal={resolvedByOrdinal}
            openIndex={openIndex}
            onToggle={activate}
          />
        ) : (
          // No inline markers — render the markdown, then a trailing marker row so
          // the answer is still navigable to its sources.
          <MarkdownView>{answer.text}</MarkdownView>
        )}
      </div>

      {!inline && resolved.length > 0 ? (
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <span className="text-xs text-foreground-muted">Sources</span>
          {resolved.map(({ result, ordinal, timeStartMs }) => (
            <CitationChip
              key={ordinal}
              index={ordinal}
              active={openIndex === ordinal}
              sourceTitle={result.title}
              {...(timeStartMs !== undefined ? { timeStartMs } : {})}
              onClick={() => activate(ordinal)}
            />
          ))}
        </div>
      ) : null}

      {/* Answer meta — permission + freshness + evidence-strength badges. */}
      {resolved.length > 0 ? (
        <div className="mt-4 flex flex-wrap items-center gap-3">
          <PermissionPill
            level={anyRestricted ? 'restricted' : 'granted'}
            label={anyRestricted ? 'Permission-checked · some fields masked' : 'Permission-checked'}
          />
          {freshest ? (
            <FreshnessPill
              label={`Freshest source ${freshnessLabel(freshest.last_indexed)
                .replace(/^Indexed /, '')
                .trim()}`}
              stale={isStale(freshest.last_indexed)}
              title={freshest.last_indexed}
            />
          ) : null}
          <span className="lc-badge lc-badge--warn">
            Evidence: {resolved.length} {resolved.length === 1 ? 'source' : 'sources'}
          </span>
        </div>
      ) : null}

      {open ? (
        <div className="mt-3">
          <SourceInspector
            title={open.result.title}
            // When the citation carries its own snippet, that IS the cited passage
            // (highlight it whole); otherwise show the result snippet with its
            // match spans highlighted.
            passage={
              open.citation.snippet
                ? { runs: [{ text: open.citation.snippet, highlight: true }] }
                : toPassageRuns(open.result.snippet, open.result.match_spans)
            }
            owner={open.result.owner ?? undefined}
            freshness={freshnessLabel(open.result.last_indexed)}
            stale={isStale(open.result.last_indexed)}
            permission={toPermissionLevel(open.result.permission)}
            onClose={() => setOpenIndex(null)}
          />
        </div>
      ) : null}
    </section>
  );
}

/**
 * Render answer text with its inline `[n]` markers replaced by clickable
 * CitationChips. Plain runs go through the sanitizing markdown pipeline so bold /
 * links / code still render — never a raw string. A marker that resolves to a
 * dropped (un-retrievable) citation is rendered as plain text, not a dead chip.
 */
function InlineAnswer({
  text,
  citationCount,
  resolvedByOrdinal,
  openIndex,
  onToggle,
}: {
  text: string;
  citationCount: number;
  resolvedByOrdinal: Map<number, ResolvedCitation>;
  openIndex: number | null;
  onToggle: (ordinal: number) => void;
}) {
  const segments = segmentAnswer(text, citationCount);
  return (
    <span>
      {segments.map((seg, i) => {
        if (seg.cite === undefined) {
          // Flow the markdown wrapper + its paragraph inline so a [n] chip sits in
          // the text rather than forcing a block break; plain runs still go through
          // the sanitizing pipeline (bold / links / code render, never raw).
          return (
            <span
              key={i}
              className="inline [&_.prose-md]:inline [&_.prose-md>p]:m-0 [&_.prose-md>p]:inline"
            >
              <MarkdownView>{seg.text}</MarkdownView>
            </span>
          );
        }
        const resolvedCite = resolvedByOrdinal.get(seg.cite);
        if (!resolvedCite) {
          // Citation was dropped (un-retrievable) — show the literal text, no chip.
          return <Fragment key={i}>{seg.text}</Fragment>;
        }
        return (
          <CitationChip
            key={i}
            index={seg.cite}
            active={openIndex === seg.cite}
            sourceTitle={resolvedCite.result.title}
            {...(resolvedCite.timeStartMs !== undefined
              ? { timeStartMs: resolvedCite.timeStartMs }
              : {})}
            onClick={() => onToggle(seg.cite!)}
          />
        );
      })}
    </span>
  );
}
