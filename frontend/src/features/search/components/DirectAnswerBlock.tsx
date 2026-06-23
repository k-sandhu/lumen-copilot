/**
 * DirectAnswerBlock (#84) — the optional cited direct answer. Present only when
 * the permitted results support it; every claim is backed by a citation that
 * references a passage present in `results` (spec 0004 INV-3) — so an answer can
 * never cite something the caller could not retrieve.
 *
 * The answer text is rendered through the ONE sanitizing markdown pipeline
 * (`MarkdownView`), never raw (frontend/AGENTS.md "Rendered, never raw"). Each
 * citation is a real CitationChip button; clicking it opens the matching
 * SourceInspector with the exact cited passage (highlighted), owner, freshness and
 * permission. A citation whose `result_id` is absent from `results` is dropped
 * defensively — the UI refuses to render an un-resolvable (un-verifiable) citation.
 */
import { useState } from 'react';
import type { DirectAnswer, SearchResult } from '@/api';
import { CitationChip, SourceInspector } from '@/ui';
import { MarkdownView } from '@/lib/markdown';
import { freshnessLabel, isStale, toPassageRuns, toPermissionLevel } from '../model/presentation';

interface DirectAnswerBlockProps {
  answer: DirectAnswer;
  /** The page's results, indexed by id, so each citation resolves to its source. */
  resultsById: Map<string, SearchResult>;
}

export function DirectAnswerBlock({ answer, resultsById }: DirectAnswerBlockProps) {
  const [openIndex, setOpenIndex] = useState<number | null>(null);

  // Only citations that resolve to a present result are renderable — a citation
  // must trace to a retrievable passage (INV-3). Drop any that don't.
  const resolved = answer.citations
    .map((c, i) => ({ citation: c, result: resultsById.get(c.result_id), ordinal: i + 1 }))
    .filter(
      (
        entry,
      ): entry is {
        citation: (typeof answer.citations)[number];
        result: SearchResult;
        ordinal: number;
      } => Boolean(entry.result),
    );

  const open = openIndex === null ? null : (resolved.find((r) => r.ordinal === openIndex) ?? null);

  return (
    <section
      aria-label="Direct answer"
      className="rounded-lg border border-accent/40 bg-accent/5 p-4"
    >
      <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-accent">Answer</h2>

      {/* Rendered through the sanitizing pipeline — never a raw markdown dump. */}
      <div className="text-sm text-foreground">
        <MarkdownView>{answer.text}</MarkdownView>
      </div>

      {resolved.length > 0 ? (
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <span className="text-xs text-foreground-muted">Sources</span>
          {resolved.map(({ result, ordinal }) => (
            <CitationChip
              key={ordinal}
              index={ordinal}
              active={openIndex === ordinal}
              sourceTitle={result.title}
              onClick={() => setOpenIndex((cur) => (cur === ordinal ? null : ordinal))}
            />
          ))}
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
