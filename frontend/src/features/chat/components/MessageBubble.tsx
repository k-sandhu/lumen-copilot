/**
 * One chat turn (user or assistant). Assistant content is ALWAYS rendered
 * through the sanitized markdown pipeline (frontend/AGENTS.md "Rendered, never
 * raw") — never a raw string, never dangerouslySetInnerHTML.
 *
 * Trust-signal re-skin (#89, ADR-0007 §1 / DESIGN.md §1/§6): an assistant turn
 * carries a model badge, a collapsible RetrievalTrace ("Looked at N sources · M
 * passages · K excluded"), and a "Sources used" strip where each cited source
 * shows a FreshnessPill. A zero-citation answer still says so honestly rather
 * than fabricating references (mission filter #2, spec 0004 INV-3). The inline
 * citation markers are the kit CitationChip; clicking any affordance opens the
 * SourceInspector (the viewer pane). All signals are derived from data the turn
 * ALREADY has — no contract change.
 *
 * Pure/presentational: streaming state, tool activity, citation collection, and
 * trust-signal derivation are owned by the hook/parent; this component renders
 * what it is given.
 */
import { memo } from 'react';
import { MarkdownView } from '@/lib/markdown';
import { cn } from '@/lib/cn';
import { CitationChip, FreshnessPill, Icon, RetrievalTrace, type TraceStep } from '@/ui';
import type { MessageRole } from '@/api';
import type { UiCitation } from '../model/citation';
import { ToolActivity } from './ToolActivity';
import type { ToolActivity as ToolActivityItem } from '../model/streamReducer';

/** Per-source freshness, keyed by documentId, derived by the parent. */
export interface SourceMeta {
  /** Relative recency label, e.g. "2d ago". */
  freshness?: string;
  /** True when the source is past its freshness window (amber). */
  stale?: boolean;
}

export interface MessageBubbleProps {
  role: MessageRole;
  content: string;
  model?: string | undefined;
  /** Friendly model label for the badge (falls back to `model`). */
  modelLabel?: string | undefined;
  citations: UiCitation[];
  /** Per-documentId freshness/staleness for the sources strip. */
  sourceMeta?: Record<string, SourceMeta>;
  /** Retrieval-trace summary line, e.g. "Looked at 3 sources · 1,204 passages". */
  traceSummary?: string;
  /** Retrieval-trace steps (shown when the trace is expanded). */
  traceSteps?: TraceStep[];
  /** Live tool activity (assistant streaming turn only). */
  tools?: ToolActivityItem[];
  /** True while this assistant turn is still streaming (shows a caret). */
  streaming?: boolean;
  /** True once a completed assistant turn produced zero citations (AC-5). */
  showNoCitationsNotice?: boolean;
  /** Open the citation in the inspector; `meta` carries the source's freshness. */
  onOpenCitation: (citation: UiCitation, meta?: SourceMeta) => void;
}

function MessageBubbleComponent({
  role,
  content,
  model,
  modelLabel,
  citations,
  sourceMeta,
  traceSummary,
  traceSteps,
  tools = [],
  streaming = false,
  showNoCitationsNotice = false,
  onOpenCitation,
}: MessageBubbleProps) {
  const isUser = role === 'user';
  const badge = !isUser ? (modelLabel ?? model) : undefined;

  return (
    <article
      className={cn('flex flex-col gap-1', isUser ? 'items-end' : 'items-stretch')}
      aria-label={`${role} message`}
    >
      <div
        className={cn(
          'max-w-[85ch] rounded-lg px-3 py-2 text-sm',
          isUser
            ? 'bg-accent/15 text-foreground'
            : 'border border-border bg-surface text-foreground',
        )}
      >
        {!isUser && badge && (
          <div className="mb-1.5 flex items-center gap-1.5">
            <span className="text-xs font-semibold text-foreground">Lumen</span>
            <span
              className="inline-flex items-center gap-1 rounded-full bg-accent/15 px-2 py-0.5 text-[11px] font-medium text-accent"
              title={model ? `Answered by ${model}` : undefined}
            >
              <Icon name="database" className="h-3 w-3" />
              {badge}
            </span>
          </div>
        )}

        {tools.length > 0 && <ToolActivity tools={tools} />}

        {!isUser && traceSummary && (
          <div className="mb-2">
            <RetrievalTrace summary={traceSummary} steps={traceSteps ?? []} />
          </div>
        )}

        {isUser ? (
          // User input is plain text — render literally, no markdown surprises.
          <p className="whitespace-pre-wrap break-words">{content}</p>
        ) : (
          <div className="min-w-0 break-words">
            <MarkdownView>{content}</MarkdownView>
            {streaming && (
              <span
                className="ml-0.5 inline-block h-3.5 w-1.5 animate-pulse bg-foreground-muted align-middle"
                aria-hidden="true"
              />
            )}
          </div>
        )}

        {!isUser && citations.length > 0 && (
          <footer className="mt-2 border-t border-border pt-2">
            <p className="mb-1 text-[11px] font-medium uppercase tracking-wide text-foreground-muted">
              Sources
            </p>
            <ol className="flex flex-col gap-1.5">
              {citations.map((citation, i) => {
                const meta = sourceMeta?.[citation.documentId];
                return (
                  <li key={citation.id} className="flex items-start gap-2 text-xs">
                    <CitationChip
                      index={i + 1}
                      sourceTitle={citation.documentName}
                      onClick={() => onOpenCitation(citation, meta)}
                    />
                    <div className="flex min-w-0 flex-col gap-0.5">
                      <button
                        type="button"
                        onClick={() => onOpenCitation(citation, meta)}
                        className="truncate text-left text-foreground-muted hover:text-foreground hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
                      >
                        {citation.documentName}
                      </button>
                      {meta?.freshness && (
                        <FreshnessPill label={meta.freshness} stale={meta.stale ?? false} />
                      )}
                    </div>
                  </li>
                );
              })}
            </ol>
          </footer>
        )}

        {!isUser && showNoCitationsNotice && (
          <p className="mt-2 border-t border-border pt-2 text-xs italic text-foreground-muted">
            No sources were cited for this answer.
          </p>
        )}
      </div>
    </article>
  );
}

export const MessageBubble = memo(MessageBubbleComponent);
