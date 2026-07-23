/**
 * The scrollable conversation pane (AC-1/AC-2). Renders persisted messages from
 * the server, then the live streaming assistant turn (token text, tool activity,
 * citations) when one is in flight. Implements every state: loading, empty,
 * error (retry), populated, and streaming-in-progress.
 *
 * Trust-signal re-skin (#89): for each assistant turn it derives the model-badge
 * label (from the models registry), the RetrievalTrace summary/steps, and the
 * per-source freshness — all from data the turn ALREADY has (citations, tools,
 * model id, timestamps); no contract change.
 *
 * Autoscroll follows new content but YIELDS to the user when they scroll up
 * (frontend/AGENTS.md "Streaming UX"). On a stream error / disconnect it shows a
 * terminal banner with a Retry affordance (AC-5).
 */
import { useEffect, useLayoutEffect, useRef } from 'react';
import { ApiError } from '@/api';
import type {
  AskUserQuestion,
  ChatAskUser,
  ChatModelInfo,
  ChatStep,
  Message,
  WsProblem,
} from '@/api';
import { ScrollArea } from '@/components/ScrollArea';
import { MessageBubble, type SourceMeta } from './MessageBubble';
import { SuggestionChips } from './SuggestionChips';
import { fromRestCitation, fromWsCitation, type UiCitation } from '../model/citation';
import {
  buildRetrievalSummary,
  isStale,
  modelBadgeLabel,
  relativeTime,
  toolActivityFromInvocations,
  usedWebSearch,
} from '../model/presentation';
import type { StreamPhase, ToolActivity, CodeRunActivity } from '../model/streamReducer';
import type { ChatCitation } from '@/api';

export interface LiveAnswer {
  phase: StreamPhase;
  text: string;
  citations: ChatCitation[];
  tools: ToolActivity[];
  /** Sandbox code runs on the in-flight turn (#232), with live stdout/stderr. */
  codeRuns: CodeRunActivity[];
  /** Live run-phase steps (spec 0006 #429) — transient, stream-only. */
  steps: ChatStep[];
  /**
   * The current tool-calling turn's live narration (event:narration,
   * ADR-0016 §6 #414) — transient muted status; never part of the answer.
   */
  narration: string | null;
  /** The clarifying question the turn ended with (spec 0006 #429), if any. */
  askUser: ChatAskUser | null;
  problem: WsProblem | null;
  model?: string | undefined;
}

/** The WS ask_user payload as the REST question shape the bubble renders. */
function questionFromAskUser(ask: ChatAskUser): AskUserQuestion {
  return {
    question: ask.question,
    options: ask.options,
    allow_free_text: ask.allowFreeText,
  };
}

export interface ChatThreadProps {
  messages: Message[];
  /** Model registry, for resolving a model id → friendly badge label. */
  models?: ChatModelInfo[];
  isLoading: boolean;
  isError: boolean;
  error: unknown;
  onRetryLoad: () => void;
  /** The in-flight assistant turn, or null when idle. */
  live: LiveAnswer | null;
  /** Retry the last send after a stream error / disconnect (AC-5). */
  onRetryStream: () => void;
  onOpenCitation: (citation: UiCitation, meta?: SourceMeta) => void;
  /**
   * Send arbitrary text as a new user message (spec 0006 #429) — the reuse seam
   * for clarifying-question options and suggestion chips. Absent ⇒ both render
   * inert (e.g. read-only contexts).
   */
  onSendText?: ((text: string) => void) | undefined;
  /** True while a send/stream is in flight — options/chips render but can't fire. */
  sendBusy?: boolean;
  /** Follow-up suggestions for the latest settled answer (spec 0006 #429). */
  suggestions?: string[];
}

/** Friendly model label from the registry, falling back to the id's tail. */
function labelForModel(
  modelId: string | undefined,
  models: ChatModelInfo[] | undefined,
): string | undefined {
  const friendly = models?.find((m) => m.id === modelId)?.label;
  return modelBadgeLabel(modelId, friendly) ?? undefined;
}

/**
 * Per-source freshness for a turn. We have no per-citation timestamp on the
 * wire, so the answer's evidence recency is the message time — surfaced so an
 * answer's evidence age is never hidden (mission "freshness").
 */
function sourceMetaFor(
  citations: UiCitation[],
  iso: string | undefined,
): Record<string, SourceMeta> {
  const freshness = relativeTime(iso);
  if (!freshness) return {};
  const stale = isStale(iso);
  const meta: Record<string, SourceMeta> = {};
  for (const c of citations) meta[c.documentId] = { freshness, stale };
  return meta;
}

export function ChatThread({
  messages,
  models,
  isLoading,
  isError,
  error,
  onRetryLoad,
  live,
  onRetryStream,
  onOpenCitation,
  onSendText,
  sendBusy = false,
  suggestions = [],
}: ChatThreadProps) {
  const endRef = useRef<HTMLDivElement | null>(null);
  const viewportRef = useRef<HTMLDivElement | null>(null);
  // Whether autoscroll is "stuck" to the bottom (user hasn't scrolled up).
  const stickRef = useRef(true);

  // Track whether the user is near the bottom; if they scroll up, stop following.
  useEffect(() => {
    const viewport = viewportRef.current;
    if (!viewport) return;

    const onScroll = () => {
      const distanceFromBottom = viewport.scrollHeight - viewport.scrollTop - viewport.clientHeight;
      stickRef.current = distanceFromBottom < 80;
    };

    viewport.addEventListener('scroll', onScroll, { passive: true });
    return () => viewport.removeEventListener('scroll', onScroll);
  }, []);

  // Total streamed code-run output length — a scalar that grows as stdout/stderr
  // chunks arrive, so autoscroll follows live code output too (#232 AC-3), not
  // just answer tokens.
  const codeOutputLen =
    live?.codeRuns.reduce((sum, r) => sum + r.stdout.length + r.stderr.length, 0) ?? 0;

  // Follow new content only while stuck to the bottom. Guard scrollIntoView —
  // it is not implemented in jsdom (tests) and may be absent in older runtimes.
  //
  // This effect stays synchronous (its call cadence is asserted immediately after
  // render in ChatThread.test.tsx). It no longer runs a layout pass PER DELTA:
  // `useChatStream` batches incoming text deltas and commits once per animation
  // frame (#493), so this component re-renders — and this autoscroll runs — at the
  // flush cadence, not the provider chunk rate. The final flush (and the terminal
  // commit) still change `live?.text` / `live?.phase`, so the view lands at the
  // bottom when the stream ends (#493 AC-5).
  useLayoutEffect(() => {
    if (stickRef.current && typeof endRef.current?.scrollIntoView === 'function') {
      endRef.current.scrollIntoView({ block: 'end' });
    }
  }, [
    messages.length,
    live?.text,
    live?.tools.length,
    live?.citations.length,
    live?.codeRuns.length,
    codeOutputLen,
    live?.phase,
    // Spec 0006 surfaces: follow step updates, a question's options, and chips.
    live?.steps,
    live?.askUser,
    suggestions.length,
  ]);

  // Reset stickiness whenever a fresh stream starts.
  useEffect(() => {
    if (live?.phase === 'streaming') stickRef.current = true;
  }, [live?.phase]);

  const showEmpty = !isLoading && !isError && messages.length === 0 && live === null;

  return (
    <ScrollArea viewportRef={viewportRef}>
      <div
        className="lc-thread"
        // The ScrollArea viewport is the scroller; this just lays out content.
      >
        {isLoading && (
          <p role="status" className="lc-thread__status">
            Loading conversation…
          </p>
        )}

        {isError && (
          <div role="alert" className="lc-thread__banner">
            <p>{error instanceof ApiError ? error.displayMessage : 'Could not load this chat.'}</p>
            <button type="button" onClick={onRetryLoad} className="lc-confirm__btn self-start">
              Retry
            </button>
          </div>
        )}

        {showEmpty && (
          <p className="lc-thread__status">
            Ask a question to start. Answers cite the documents they draw from.
          </p>
        )}

        {messages.map((message, index) => {
          const citations = (message.citations ?? []).map(fromRestCitation);
          const isAssistant = message.role === 'assistant';
          // Clarifying-question wiring (spec 0006 #429): options are clickable
          // only on the conversation's LAST message with nothing in flight; an
          // answered question highlights the choice from the following user turn.
          const nextMessage = messages[index + 1];
          const isLast = index === messages.length - 1;
          const questionActive = isLast && live === null && !sendBusy && onSendText !== undefined;
          const chosenAnswer =
            message.question && nextMessage?.role === 'user' ? nextMessage.content : undefined;
          // The persisted governed tool trace (#377), rendered through the SAME
          // ToolActivity badges as a live turn — an answer's tool activity stays
          // visible after reload, not only in the audit log.
          const tools = isAssistant
            ? toolActivityFromInvocations(message.tool_invocations ?? [])
            : [];
          const trace = isAssistant
            ? buildRetrievalSummary(citations, [])
            : { summary: '', steps: [], hasContent: false };
          // The footer's "answered <ago>" (#120) is the message/answer time — when
          // this answer was produced, never presented as source provenance.
          const answeredAt = isAssistant
            ? (relativeTime(message.created_at) ?? undefined)
            : undefined;
          return (
            <MessageBubble
              key={message.id}
              role={message.role}
              content={message.content}
              model={message.model}
              modelLabel={isAssistant ? labelForModel(message.model, models) : undefined}
              citations={citations}
              sourceMeta={isAssistant ? sourceMetaFor(citations, message.created_at) : undefined}
              traceSummary={isAssistant && trace.hasContent ? trace.summary : undefined}
              traceSteps={trace.steps}
              tools={tools}
              question={message.question}
              questionActive={questionActive}
              chosenAnswer={chosenAnswer}
              onChooseOption={onSendText}
              answeredAt={answeredAt}
              showNoCitationsNotice={isAssistant && citations.length === 0}
              // Web usage on a persisted turn is derived from citations only — the
              // persisted trace records that web_search RAN, while the disclosure
              // means "web results were used"; a run with no web citation stays
              // conservative (#221).
              webUsed={isAssistant && usedWebSearch(citations, [])}
              onOpenCitation={onOpenCitation}
            />
          );
        })}

        {live &&
          (() => {
            const liveCitations = live.citations.map(fromWsCitation);
            const trace = buildRetrievalSummary(liveCitations, live.tools);
            // Once the runtime reports the answer settled (finalize/suggest
            // phases, spec 0006), the text is final: drop the caret + live
            // region even though suggestion generation still runs.
            const answerSettled = live.steps.some((s) => s.key === 'finalize');
            const liveQuestion = live.askUser ? questionFromAskUser(live.askUser) : undefined;
            // Transient narration (#414): shown only while the answer is still
            // streaming and there is no answer text yet — the model narrating
            // its tool work. Cleared naturally once deltas arrive or the
            // stream settles; never rendered into the message body.
            const showNarration =
              live.phase === 'streaming' && !answerSettled && !live.text && !!live.narration;
            return (
              <>
                {showNarration && (
                  <div
                    className="chat-narration text-sm italic text-muted-foreground px-4 py-1"
                    role="status"
                    aria-live="polite"
                    data-testid="live-narration"
                  >
                    {live.narration}
                  </div>
                )}
                <MessageBubble
                  role="assistant"
                  // A question turn streams no deltas — the question text IS the
                  // turn's content (matches what persists, spec 0006).
                  content={live.text || (live.askUser?.question ?? '')}
                  model={live.model}
                  modelLabel={labelForModel(live.model, models)}
                  citations={liveCitations}
                  traceSummary={trace.hasContent ? trace.summary : undefined}
                  traceSteps={trace.steps}
                  tools={live.tools}
                  codeRuns={live.codeRuns}
                  steps={live.steps}
                  question={liveQuestion}
                  // The just-asked question is answerable the moment the stream
                  // settles; the composer stays available as the free-text path.
                  questionActive={
                    live.phase === 'done' && live.askUser !== null && onSendText !== undefined
                  }
                  onChooseOption={onSendText}
                  streaming={live.phase === 'streaming' && !answerSettled}
                  // A just-settled live answer was produced now → "answered Just now".
                  answeredAt={live.phase === 'done' ? 'Just now' : undefined}
                  showNoCitationsNotice={
                    live.phase === 'done' && live.citations.length === 0 && !live.askUser
                  }
                  // The disclosure only shows on a settled turn (E3-12) — while
                  // streaming, a web lookup may still be in flight.
                  webUsed={live.phase === 'done' && usedWebSearch(liveCitations, live.tools)}
                  onOpenCitation={onOpenCitation}
                />
                {live.phase === 'error' && (
                  <div role="alert" className="lc-thread__banner">
                    <p>
                      {live.problem?.detail ?? live.problem?.title ?? 'The answer stream failed.'}
                    </p>
                    <button
                      type="button"
                      onClick={onRetryStream}
                      className="lc-confirm__btn self-start"
                    >
                      Retry
                    </button>
                  </div>
                )}
              </>
            );
          })()}

        {/* Follow-up chips (spec 0006 #429): under the latest settled answer;
            clicking sends the question. Never shown mid-stream. */}
        {onSendText && suggestions.length > 0 && live?.phase !== 'streaming' && (
          <SuggestionChips suggestions={suggestions} disabled={sendBusy} onPick={onSendText} />
        )}

        <div ref={endRef} />
      </div>
    </ScrollArea>
  );
}
