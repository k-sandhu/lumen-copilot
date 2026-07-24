/**
 * #493 AC-5 — autoscroll no longer runs a layout pass per delta.
 *
 * Drives the real `useChatStream` hook (via an injected fake socket) into a real
 * `ChatThread`, streaming many text deltas each in its OWN task (React auto-batching
 * cannot coalesce separate `onmessage` frames). Because deltas are accumulated at
 * the hook ingress and committed once per animation frame, `ChatThread` re-renders
 * per FLUSH, not per delta — so its `scrollIntoView` layout runs a bounded number
 * of times regardless of how many deltas arrive, while still landing at the bottom
 * once the batch flushes. rAF is faked by `vi.useFakeTimers`, so flushes are driven
 * deterministically by advancing timers (never real waiting).
 */
import { afterEach, describe, it, expect, vi } from 'vitest';
import { act, render } from '@testing-library/react';
import type { WsClientOptions, WsEnvelope } from '@/api';
import { useChatStream, type ClientFactory, type StreamSocket } from '../model/useChatStream';
import { ChatThread, type LiveAnswer } from './ChatThread';

const originalScrollIntoView = Element.prototype.scrollIntoView;

afterEach(() => {
  vi.useRealTimers();
  if (originalScrollIntoView) {
    Object.defineProperty(Element.prototype, 'scrollIntoView', {
      configurable: true,
      value: originalScrollIntoView,
    });
  } else {
    Reflect.deleteProperty(Element.prototype, 'scrollIntoView');
  }
});

function mockScrollIntoView() {
  const scrollIntoView = vi.fn();
  Object.defineProperty(Element.prototype, 'scrollIntoView', {
    configurable: true,
    value: scrollIntoView,
  });
  return scrollIntoView;
}

class FakeSocket implements StreamSocket {
  opts: WsClientOptions;
  closed = false;
  constructor(opts: WsClientOptions) {
    this.opts = opts;
  }
  connect(): void {
    this.opts.onStateChange?.('open');
  }
  close(): void {
    if (this.closed) return;
    this.closed = true;
    this.opts.onStateChange?.('closed');
  }
  getState() {
    return this.closed ? ('closed' as const) : ('open' as const);
  }
  emit(envelope: WsEnvelope): void {
    this.opts.onEnvelope?.(envelope);
  }
}

function harness() {
  let socket: FakeSocket | null = null;
  const makeClient: ClientFactory = (opts) => {
    socket = new FakeSocket(opts);
    return socket;
  };
  return { makeClient, get: () => socket! };
}

const SID = 'stream-scroll';

function StreamedThread({ makeClient }: { makeClient: ClientFactory }) {
  const stream = useChatStream({ streamId: SID, makeClient });
  const live: LiveAnswer = {
    phase: stream.phase,
    text: stream.text,
    citations: stream.citations,
    tools: stream.tools,
    codeRuns: stream.codeRuns,
    steps: stream.steps,
    narration: stream.narration?.text ?? null,
    askUser: stream.askUser,
    problem: stream.problem,
  };
  return (
    <ChatThread
      messages={[]}
      isLoading={false}
      isError={false}
      error={null}
      onRetryLoad={() => {}}
      live={live}
      onRetryStream={() => {}}
      onOpenCitation={() => {}}
    />
  );
}

describe('streaming autoscroll throttling (#493 AC-5)', () => {
  it('runs one autoscroll layout per flush, not one per delta', () => {
    vi.useFakeTimers();
    const scrollIntoView = mockScrollIntoView();
    const h = harness();
    render(<StreamedThread makeClient={h.makeClient} />);

    act(() => {
      h.get().emit({ type: 'start', streamId: SID, seq: 0, data: {} });
    });
    // Ignore the mount/start layout passes — measure only what the deltas cost.
    scrollIntoView.mockClear();

    // 100 deltas, each delivered in its own task (fresh act ⇒ React cannot batch).
    for (let i = 1; i <= 100; i++) {
      act(() => {
        h.get().emit({ type: 'delta', streamId: SID, seq: i, data: { text: 'x' } });
      });
    }
    // Buffered — no re-render, so no autoscroll layout ran for the 100 deltas.
    expect(scrollIntoView).not.toHaveBeenCalled();

    // The coalesced flush commits the whole batch: exactly one layout pass, and
    // it lands at the bottom with the full text.
    act(() => {
      vi.advanceTimersByTime(50);
    });
    expect(scrollIntoView).toHaveBeenCalledTimes(1);
  });

  it('still lands at the bottom when the stream ends', () => {
    vi.useFakeTimers();
    const scrollIntoView = mockScrollIntoView();
    const h = harness();
    render(<StreamedThread makeClient={h.makeClient} />);

    act(() => {
      h.get().emit({ type: 'start', streamId: SID, seq: 0, data: {} });
      h.get().emit({ type: 'delta', streamId: SID, seq: 1, data: { text: 'Answer ' } });
      h.get().emit({ type: 'delta', streamId: SID, seq: 2, data: { text: 'done.' } });
    });
    scrollIntoView.mockClear();

    // The terminal flushes the pending buffer first, then settles — the final
    // content is scrolled into view (a layout pass fires on the terminal commit).
    act(() => {
      h.get().emit({
        type: 'done',
        streamId: SID,
        seq: 3,
        data: { messageId: 'm', finishReason: 'stop', citationCount: 0 },
      });
    });
    expect(scrollIntoView).toHaveBeenCalled();
  });
});
