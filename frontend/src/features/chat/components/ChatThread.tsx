/**
 * The scrollable conversation pane (AC-1/AC-2). Renders persisted messages from
 * the server, then the live streaming assistant turn (token text, tool activity,
 * citations) when one is in flight. Implements every state: loading, empty,
 * error (retry), populated, and streaming-in-progress.
 *
 * Autoscroll follows new content but YIELDS to the user when they scroll up
 * (frontend/AGENTS.md "Streaming UX"). On a stream error / disconnect it shows a
 * terminal banner with a Retry affordance (AC-5).
 */
import { useEffect, useLayoutEffect, useRef } from 'react';
import { ApiError } from '@/api';
import type { Message, WsProblem } from '@/api';
import { ScrollArea } from '@/components/ScrollArea';
import { MessageBubble } from './MessageBubble';
import { fromRestCitation, fromWsCitation, type UiCitation } from '../model/citation';
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
  isLoading: boolean;
  isError: boolean;
  error: unknown;
  onRetryLoad: () => void;
  /** The in-flight assistant turn, or null when idle. */
  live: LiveAnswer | null;
  /** Retry the last send after a stream error / disconnect (AC-5). */
  onRetryStream: () => void;
  onOpenCitation: (citation: UiCitation) => void;
}

export function ChatThread({
  messages,
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

        {messages.map((message) => (
          <MessageBubble
            key={message.id}
            role={message.role}
            content={message.content}
            model={message.model}
            citations={(message.citations ?? []).map(fromRestCitation)}
            showNoCitationsNotice={message.role === 'assistant' && (message.citations ?? []).length === 0}
            onOpenCitation={onOpenCitation}
          />
        ))}

        {live && (
          <>
            <MessageBubble
              role="assistant"
              content={live.text}
              model={live.model}
              citations={live.citations.map(fromWsCitation)}
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
        )}

        <div ref={endRef} />
      </div>
    </ScrollArea>
  );
}
