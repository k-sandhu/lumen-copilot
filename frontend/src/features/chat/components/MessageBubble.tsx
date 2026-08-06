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
import type { AskUserQuestion, ChatStep, MessageRole } from '@/api';
import { hostOf, isSafeHttpUrl, type UiCitation } from '../model/citation';
import { groupCitationsByDocument, partitionCitations } from '../model/presentation';
import { ToolActivity } from './ToolActivity';
import { AnswerFooter } from './AnswerFooter';
import { AskUserOptions } from './AskUserOptions';
import { StepTimeline } from './StepTimeline';
import { CodeRunPanel } from '@/features/codeRuns';
import { artifactHref } from '@/features/artifacts';
import type { ToolActivity as ToolActivityItem, CodeRunActivity } from '../model/streamReducer';

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
  /**
   * Sandbox code runs on this turn (#232). While streaming these carry live
   * stdout/stderr folded from the WS; each renders an inspectable CodeRunPanel
   * that also backfills the full record from GET /code-runs/{id}. Only the live
   * turn passes this today — the chat-message wire carries no run reference yet,
   * so a persisted turn renders none (a past run is inspected via CodeRunDetail).
   */
  codeRuns?: CodeRunActivity[];
  /** True while this assistant turn is still streaming (shows a caret). */
  streaming?: boolean;
  /**
   * Live run-phase steps (spec 0006 #429) — the streaming turn only. Rendered
   * as a timeline while no answer text has arrived (the pre-token void), then
   * dropped: flowing text is its own progress signal.
   */
  steps?: ChatStep[];
  /** The clarifying question this turn ended with, if any (spec 0006 #429). */
  question?: AskUserQuestion | undefined;
  /** Whether the question's options are clickable (last message, nothing in flight). */
  questionActive?: boolean;
  /** The following user turn's text — highlights the chosen option on answered questions. */
  chosenAnswer?: string | undefined;
  /** Send an option label as the next user message (spec 0006 #429). */
  onChooseOption?: ((label: string) => void) | undefined;
  /**
   * When the answer was produced (e.g. "2d ago"), for the footer's
   * "answered <ago>" signal. Derived from the message timestamp; omitted if none.
   * This is the ANSWER time, not source freshness/last-indexed (#120 GUARD).
   */
  answeredAt?: string | undefined;
  /** True once a completed assistant turn produced zero citations (AC-5). */
  showNoCitationsNotice?: boolean;
  /**
   * True when this answer used web search (#221, E3-12) — a web citation was
   * present or the `web_search` tool ran. Drives the "web sources were used"
   * disclosure. Only meaningful on a settled assistant turn.
   */
  webUsed?: boolean;
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
  codeRuns = [],
  streaming = false,
  steps = [],
  question,
  questionActive = false,
  chosenAnswer,
  onChooseOption,
  answeredAt,
  showNoCitationsNotice = false,
  webUsed = false,
  onOpenCitation,
}: MessageBubbleProps) {
  const isUser = role === 'user';
  const badge = !isUser ? (modelLabel ?? model) : undefined;
  // Web citations render distinctly from corpus-document citations (#221 AC-2).
  const { web: webCitations, documents: docCitations } = partitionCitations(citations);

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
            {/* Live run-phase timeline (spec 0006): only in the pre-token void —
                once answer text flows, the text itself shows progress. */}
            {streaming && content.length === 0 && steps.length > 0 && (
              <StepTimeline steps={steps} />
            )}

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
              <MarkdownView className="lc-answer" streaming={streaming}>
                {content}
              </MarkdownView>
              {streaming && <span className="lc-caret" aria-hidden="true" />}
            </div>

            {/* Clarifying-question options (spec 0006 #429): clickable while this
                is the conversation's last turn, inert (chosen highlighted) after. */}
            {question && (
              <AskUserOptions
                question={question}
                active={questionActive}
                chosenAnswer={chosenAnswer}
                onChoose={onChooseOption}
              />
            )}

            {/* Sandbox code runs (#232, E3-7): computed results separated from the
                narrative, each inspectable (code + streamed output + result +
                artifacts). Rendered after the answer so the prose leads. */}
            {codeRuns.length > 0 && (
              <div className="mt-1" aria-label="Code runs">
                {codeRuns.map((run) => (
                  <CodeRunPanel
                    key={run.runId}
                    runId={run.runId}
                    activity={run}
                    resolveArtifactHref={artifactHref}
                  />
                ))}
              </div>
            )}

            {docCitations.length > 0 && (
              <>
                <p className="lc-sources__label">Sources used</p>
                {/* One card per DOCUMENT (#248): a grounded answer often cites
                    several passages of the same file, so grouping avoids listing
                    the document N times. Each passage keeps its FLAT number so the
                    inline [n] markers still line up. */}
                <ol className="lc-sources">
                  {groupCitationsByDocument(docCitations).map((group) => {
                    const meta = sourceMeta?.[group.documentId];
                    return (
                      <li key={group.documentId} className="lc-source-row">
                        <span className="lc-source-row__main">
                          {/* A revoked source keeps its place in the list: the
                              answer was genuinely grounded in it, and quietly
                              dropping the row would make the claim look
                              unsourced (#536). */}
                          <span
                            className={
                              group.redacted
                                ? 'lc-source-row__title lc-source-row__title--redacted'
                                : 'lc-source-row__title'
                            }
                            title={group.redacted ? undefined : group.documentName}
                          >
                            {group.redacted ? 'Source no longer available' : group.documentName}
                          </span>
                          {meta?.freshness && (
                            <span className="lc-source-row__sub">
                              <FreshnessPill label={meta.freshness} stale={meta.stale ?? false} />
                            </span>
                          )}
                        </span>
                        {/* One button per cited passage, numbered by its flat
                            index so it reads as the numbered reference and opens
                            that passage in the inspector. */}
                        <span
                          className="lc-source-row__passages"
                          role="group"
                          aria-label={
                            group.redacted
                              ? 'Cited passages from a source you can no longer access'
                              : `Cited passages from ${group.documentName}`
                          }
                        >
                          {group.passages.map((p) => (
                            <button
                              key={p.citation.id}
                              type="button"
                              className="lc-source-row__num-btn"
                              disabled={group.redacted}
                              aria-label={
                                group.redacted
                                  ? `Citation ${p.number}: source no longer available`
                                  : `Citation ${p.number}: ${group.documentName}`
                              }
                              onClick={() => onOpenCitation(p.citation, meta)}
                            >
                              {p.number}
                            </button>
                          ))}
                        </span>
                        {/* Honest by construction: the backend re-checks INV-2 on
                            every READ, not just when the citation was written
                            (#536), so a source shown with its name is one this
                            user may still see — and one they may not comes back
                            stripped, flagged, and rendered as unavailable. */}
                        <PermissionPill level="granted" />
                      </li>
                    );
                  })}
                </ol>
              </>
            )}

            {webCitations.length > 0 && (
              <WebSources citations={webCitations} onOpen={onOpenCitation} />
            )}

            {/* "Web sources were used" disclosure (#221, E3-12): discloses which
                modes an answer used, shown on a settled turn that ran web search. */}
            {webUsed && (
              <p className="lc-web-disclosure" role="note">
                <Icon name="globe" />
                Web sources were used for this answer.
              </p>
            )}

            {/* A clarifying question makes no claims — the zero-citation notice
                would be noise under it (spec 0006 INV-3 posture). */}
            {showNoCitationsNotice && !question && (
              <p className="lc-no-sources">No sources were cited for this answer.</p>
            )}

            {/*
              Answer-bubble footer (#120) — only on a settled assistant turn (not
              while streaming, and only once content exists). "Permission-checked"
              shows when the answer is grounded in ≥1 cited source (honest: never a
              bare claim).
            */}
            {/* No footer under a clarifying question — thumbs/copy on a question
                the user is about to answer reads as clutter (spec 0006). */}
            {!streaming && content.length > 0 && !question && (
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

/**
 * Web citations, rendered DISTINCTLY from corpus-document sources (#221 AC-2):
 * each shows the page title, its host (+ a favicon affordance), the cited
 * snippet, and an external-link that opens the page in a new tab with
 * `rel="noopener noreferrer"`. Clicking the row opens the web-source inspector
 * pane (host + snippet + safe link) — the same click-through corpus sources get.
 * A URL that isn't safe http(s) renders no outbound link (never an unsafe link).
 */
function WebSources({
  citations,
  onOpen,
}: {
  citations: UiCitation[];
  onOpen: (citation: UiCitation) => void;
}) {
  return (
    <>
      <p className="lc-sources__label">Web sources</p>
      <ul className="lc-web-sources" aria-label="Web sources">
        {citations.map((citation) => {
          const host = hostOf(citation.url);
          const title = citation.webTitle ?? citation.documentName ?? host ?? 'Web result';
          const safeHref = isSafeHttpUrl(citation.url) ? citation.url : undefined;
          return (
            <li key={citation.id} className="lc-web-source">
              <button
                type="button"
                className="lc-web-source__btn"
                aria-label={`Web source: ${title}`}
                onClick={() => onOpen(citation)}
              >
                <span className="lc-web-source__icon" aria-hidden="true">
                  {/* Favicon affordance: the site's favicon by host, with the
                      globe glyph as the always-present fallback beneath it. */}
                  {host ? (
                    <img
                      src={`https://www.google.com/s2/favicons?domain=${encodeURIComponent(host)}&sz=32`}
                      alt=""
                      width={16}
                      height={16}
                      loading="lazy"
                      referrerPolicy="no-referrer"
                      className="lc-web-source__favicon"
                    />
                  ) : null}
                  <Icon name="globe" className="lc-web-source__globe" />
                </span>
                <span className="lc-web-source__main">
                  <span className="lc-web-source__title">{title}</span>
                  {host ? <span className="lc-web-source__host">{host}</span> : null}
                  {citation.snippet.trim() ? (
                    <span className="lc-web-source__snippet">{citation.snippet}</span>
                  ) : null}
                </span>
              </button>
              {safeHref ? (
                <a
                  className="lc-web-source__link"
                  href={safeHref}
                  target="_blank"
                  rel="noopener noreferrer"
                  aria-label={`Open ${title} in a new tab`}
                >
                  <Icon name="link" />
                </a>
              ) : null}
            </li>
          );
        })}
      </ul>
    </>
  );
}

export const MessageBubble = memo(MessageBubbleComponent);
