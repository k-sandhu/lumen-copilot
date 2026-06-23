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
import type { ChatModelInfo, Message, WsProblem } from '@/api';
import { ScrollArea } from '@/components/ScrollArea';
import { MessageBubble, type SourceMeta } from './MessageBubble';
import { fromRestCitation, fromWsCitation, type UiCitation } from '../model/citation';
import {
  buildRetrievalSummary,
  isStale,
  modelBadgeLabel,
  relativeTime,
} from '../model/presentation';
import type { StreamPhase, ToolActivity } from '../model/streamReducer';
import type { ChatCitation } from '@/api';

export interface LiveAnswer {
  phase: StreamPhase;
  text: string;
  citations: ChatCitation[];
  tools: ToolActivity[];
  problem: WsProblem | null;
  model?: string | undefined;
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
function sourceMetaFor(citations: UiCitation[], iso: string | undefined): Record<string, SourceMeta> {
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
}: ChatThreadProps) {
  const endRef = useRef<HTMLDivElement | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  // Whether autoscroll is "stuck" to the bottom (user hasn't scrolled up).
  const stickRef = useRef(true);

  // Track whether the user is near the bottom; if they scroll up, stop following.
  function onScroll(e: React.UIEvent<HTMLDivElement>) {
    const el = e.currentTarget;
    const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    stickRef.current = distanceFromBottom < 80;
  }

  // Follow new content only while stuck to the bottom. Guard scrollIntoView —
  // it is not implemented in jsdom (tests) and may be absent in older runtimes.
  useLayoutEffect(() => {
    if (stickRef.current && typeof endRef.current?.scrollIntoView === 'function') {
      endRef.current.scrollIntoView({ block: 'end' });
    }
  }, [messages.length, live?.text, live?.tools.length, live?.citations.length, live?.phase]);

  // Reset stickiness whenever a fresh stream starts.
  useEffect(() => {
    if (live?.phase === 'streaming') stickRef.current = true;
  }, [live?.phase]);

  const showEmpty = !isLoading && !isError && messages.length === 0 && live === null;

  return (
    <ScrollArea>
      <div
        ref={scrollRef}
        onScroll={onScroll}
        className="flex flex-col gap-4 p-4"
        // The ScrollArea viewport is the scroller; this just lays out content.
      >
        {isLoading && (
          <p role="status" className="text-sm text-foreground-muted">
            Loading conversation…
          </p>
        )}

        {isError && (
          <div role="alert" className="rounded-md border border-danger/40 bg-danger/10 p-3 text-sm">
            <p className="text-danger">
              {error instanceof ApiError ? error.displayMessage : 'Could not load this chat.'}
            </p>
            <button
              type="button"
              onClick={onRetryLoad}
              className="mt-2 rounded-md border border-border bg-surface px-3 py-1.5 hover:bg-surface-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
            >
              Retry
            </button>
          </div>
        )}

        {showEmpty && (
          <p className="text-sm text-foreground-muted">
            Ask a question to start. Answers cite the documents they draw from.
          </p>
        )}

        {messages.map((message) => {
          const citations = (message.citations ?? []).map(fromRestCitation);
          const isAssistant = message.role === 'assistant';
          const trace = isAssistant
            ? buildRetrievalSummary(citations, [])
            : { summary: '', steps: [], hasContent: false };
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
              showNoCitationsNotice={isAssistant && citations.length === 0}
              onOpenCitation={onOpenCitation}
            />
          );
        })}

        {live &&
          (() => {
            const liveCitations = live.citations.map(fromWsCitation);
            const trace = buildRetrievalSummary(liveCitations, live.tools);
            return (
              <>
                <MessageBubble
                  role="assistant"
                  content={live.text}
                  model={live.model}
                  modelLabel={labelForModel(live.model, models)}
                  citations={liveCitations}
                  traceSummary={trace.hasContent ? trace.summary : undefined}
                  traceSteps={trace.steps}
                  tools={live.tools}
                  streaming={live.phase === 'streaming'}
                  showNoCitationsNotice={live.phase === 'done' && live.citations.length === 0}
                  onOpenCitation={onOpenCitation}
                />
                {live.phase === 'error' && (
                  <div
                    role="alert"
                    className="rounded-md border border-danger/40 bg-danger/10 p-3 text-sm"
                  >
                    <p className="text-danger">
                      {live.problem?.detail ?? live.problem?.title ?? 'The answer stream failed.'}
                    </p>
                    <button
                      type="button"
                      onClick={onRetryStream}
                      className="mt-2 rounded-md border border-border bg-surface px-3 py-1.5 hover:bg-surface-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
                    >
                      Retry
                    </button>
                  </div>
                )}
              </>
            );
          })()}

        <div ref={endRef} />
      </div>
    </ScrollArea>
  );
}
