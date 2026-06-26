/**
 * One chat turn (user or assistant), rendered as an avatar + turn-row matching
 * the canonical chat design (docs/wireframes/chat.html, DESIGN.md §1/§6) on the
 * production token system (issue #136). Assistant content is ALWAYS rendered
 * through the sanitized markdown pipeline (frontend/AGENTS.md "Rendered, never
 * raw") — never a raw string, never dangerouslySetInnerHTML.
 *
 * Trust-signal layout (#89/#120): an assistant turn carries a model badge, a
 * collapsible RetrievalTrace ("Looked at N sources · M passages · K excluded"),
 * a "Sources used" strip where each cited source shows its number (kit
 * CitationChip → opens the SourceInspector), a FreshnessPill, and a
 * PermissionPill (honest by construction: the backend only returns sources the
 * caller may see — spec 0004 INV-2). A zero-citation answer still says so
 * honestly. All signals are derived from data the turn ALREADY has — no contract
 * change.
 *
 * Pure/presentational: streaming state, tool activity, citation collection, and
 * trust-signal derivation are owned by the hook/parent; this component renders
 * what it is given.
 */
import { memo } from 'react';
import { MarkdownView } from '@/lib/markdown';
import { FreshnessPill, Icon, PermissionPill, RetrievalTrace, type TraceStep } from '@/ui';
import type { MessageRole } from '@/api';
import type { UiCitation } from '../model/citation';
import { ToolActivity } from './ToolActivity';
import { AnswerFooter } from './AnswerFooter';
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
  /**
   * When the answer was produced (e.g. "2d ago"), for the footer's
   * "answered <ago>" signal. Derived from the message timestamp; omitted if none.
   * This is the ANSWER time, not source freshness/last-indexed (#120 GUARD).
   */
  answeredAt?: string | undefined;
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
  answeredAt,
  showNoCitationsNotice = false,
  onOpenCitation,
}: MessageBubbleProps) {
  const isUser = role === 'user';
  const badge = !isUser ? (modelLabel ?? model) : undefined;

  return (
    <article
      className={`lc-turn ${isUser ? 'lc-turn--user' : 'lc-turn--assistant'}`}
      aria-label={`${role} message`}
    >
      <div className="lc-turn__who">
        {isUser ? (
          <div className="lc-turn__avatar" aria-hidden="true">
            <Icon name="user" />
          </div>
        ) : (
          <div className="lc-turn__logo" aria-hidden="true">
            <Icon name="sparkles" />
          </div>
        )}
      </div>

      <div className="lc-turn__body">
        <div className="lc-turn__name">
          {isUser ? 'You' : 'Lumen'}
          {!isUser && badge && (
            <span className="lc-model-badge" title={model ? `Answered by ${model}` : undefined}>
              <Icon name="database" />
              {badge}
            </span>
          )}
        </div>

        {isUser ? (
          // User input is plain text — render literally, no markdown surprises.
          <div className="lc-turn__text">{content}</div>
        ) : (
          <>
            {tools.length > 0 && <ToolActivity tools={tools} />}

            {traceSummary && (
              <div className="mb-2">
                <RetrievalTrace summary={traceSummary} steps={traceSteps ?? []} />
              </div>
            )}

            {/* While streaming, the answer is a polite, non-atomic live region so
                a screen reader announces tokens as they arrive (without spamming
                on every token, and without re-announcing the whole answer). A
                settled turn drops the live-region attributes so the finished
                answer isn't read out a second time. */}
            <div
              className="min-w-0 break-words"
              {...(streaming
                ? {
                    role: 'log',
                    'aria-live': 'polite' as const,
                    'aria-atomic': false,
                    'aria-label': 'Assistant answer',
                  }
                : {})}
            >
              <MarkdownView className="lc-answer">{content}</MarkdownView>
              {streaming && <span className="lc-caret" aria-hidden="true" />}
            </div>

            {citations.length > 0 && (
              <>
                <p className="lc-sources__label">Sources used</p>
                <ol className="lc-sources">
                  {citations.map((citation, i) => {
                    const meta = sourceMeta?.[citation.documentId];
                    return (
                      <li key={citation.id} className="lc-source-row">
                        {/* One button per source (single tab stop) named like the
                            kit CitationChip — "Citation N: <source>" — so it reads
                            as the numbered reference and opens the inspector. */}
                        <button
                          type="button"
                          className="lc-source-row__btn"
                          aria-label={`Citation ${i + 1}: ${citation.documentName}`}
                          onClick={() => onOpenCitation(citation, meta)}
                        >
                          <span className="lc-source-row__num">{i + 1}</span>
                          <span className="lc-source-row__main">
                            <span className="lc-source-row__title">{citation.documentName}</span>
                            {meta?.freshness && (
                              <span className="lc-source-row__sub">
                                <FreshnessPill label={meta.freshness} stale={meta.stale ?? false} />
                              </span>
                            )}
                          </span>
                        </button>
                        {/* Honest by construction: the backend only returns sources
                            the caller may see (spec 0004 INV-2), so a cited source
                            is one this user has access to. Non-interactive, so it
                            stays outside the button. */}
                        <PermissionPill level="granted" />
                      </li>
                    );
                  })}
                </ol>
              </>
            )}

            {showNoCitationsNotice && (
              <p className="lc-no-sources">No sources were cited for this answer.</p>
            )}

            {/*
              Answer-bubble footer (#120) — only on a settled assistant turn (not
              while streaming, and only once content exists). "Permission-checked"
              shows when the answer is grounded in ≥1 cited source (honest: never a
              bare claim).
            */}
            {!streaming && content.length > 0 && (
              <AnswerFooter
                answerText={content}
                permissionChecked={citations.length > 0}
                answeredAt={answeredAt}
              />
            )}
          </>
        )}
      </div>
    </article>
  );
}

export const MessageBubble = memo(MessageBubbleComponent);
