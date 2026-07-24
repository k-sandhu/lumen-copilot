/**
 * useChatStream — subscribes to one answer stream via an INJECTED fake socket
 * (no network). Covers the streaming lifecycle (AC-1), done → onDone (AC-2),
 * terminal error and unexpected-disconnect → terminal (AC-5), and cancel.
 */
import { afterEach, describe, it, expect, vi } from 'vitest';
import { act, renderHook, waitFor } from '@testing-library/react';
import { useChatStream, type ClientFactory, type StreamSocket } from './useChatStream';
import type { WsClientOptions, WsEnvelope } from '@/api';

/** A controllable fake WS client that records callbacks and lets tests push. */
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
  /** Simulate a server-side drop without a terminal envelope. */
  drop(): void {
    this.opts.onStateChange?.('closed');
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

const SID = 'stream-xyz';

describe('useChatStream', () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it('is idle with no socket when streamId is null', () => {
    const h = harness();
    const { result } = renderHook(() =>
      useChatStream({ streamId: null, makeClient: h.makeClient }),
    );
    expect(result.current.phase).toBe('idle');
  });

  it('streams delta tokens and citations, then fires onDone (AC-1/AC-2)', async () => {
    const h = harness();
    const onDone = vi.fn();
    const { result } = renderHook(() =>
      useChatStream({ streamId: SID, makeClient: h.makeClient, onDone }),
    );

    act(() => {
      h.get().emit({
        type: 'start',
        streamId: SID,
        seq: 0,
        data: { sessionId: 's', messageId: 'm', model: 'openai/gpt' },
      });
      h.get().emit({ type: 'delta', streamId: SID, seq: 1, data: { text: 'Per ' } });
      h.get().emit({ type: 'delta', streamId: SID, seq: 2, data: { text: 'the doc…' } });
      h.get().emit({
        type: 'event',
        streamId: SID,
        seq: 3,
        name: 'citation',
        data: {
          id: 'c1',
          documentId: 'd1',
          documentName: 'f.pdf',
          chunkId: 'k1',
          snippet: 's',
          charStart: 0,
          charEnd: 5,
        },
      });
    });

    expect(result.current.text).toBe('Per the doc…');
    expect(result.current.citations).toHaveLength(1);
    expect(onDone).not.toHaveBeenCalled();

    act(() => {
      h.get().emit({
        type: 'done',
        streamId: SID,
        seq: 4,
        data: { messageId: 'm', finishReason: 'stop', citationCount: 1 },
      });
    });

    await waitFor(() => expect(result.current.phase).toBe('done'));
    expect(onDone).toHaveBeenCalledTimes(1);
  });

  it('treats a WS error envelope as terminal (AC-5)', () => {
    const h = harness();
    const { result } = renderHook(() => useChatStream({ streamId: SID, makeClient: h.makeClient }));
    act(() => {
      h.get().emit({ type: 'start', streamId: SID, seq: 0, data: {} });
      h.get().emit({
        type: 'error',
        streamId: SID,
        seq: 1,
        problem: { title: 'boom', status: 500, code: 'x' },
      });
    });
    expect(result.current.phase).toBe('error');
    expect(result.current.problem?.code).toBe('x');
    expect(h.get().closed).toBe(true);
  });

  it('treats an unexpected disconnect (no terminal) as terminal error (AC-5)', () => {
    const h = harness();
    const { result } = renderHook(() => useChatStream({ streamId: SID, makeClient: h.makeClient }));
    act(() => {
      h.get().emit({ type: 'start', streamId: SID, seq: 0, data: {} });
      h.get().emit({ type: 'delta', streamId: SID, seq: 1, data: { text: 'half' } });
      h.get().drop();
    });
    expect(result.current.phase).toBe('error');
    expect(result.current.problem?.code).toBe('stream_disconnected');
    expect(result.current.text).toBe('half');
  });

  it('closes the client on an unexpected disconnect so it cannot zombie-reconnect (#273)', () => {
    const h = harness();
    renderHook(() => useChatStream({ streamId: SID, makeClient: h.makeClient }));
    act(() => {
      h.get().emit({ type: 'start', streamId: SID, seq: 0, data: {} });
      h.get().drop(); // server-side drop with no terminal envelope
    });
    // The hook must close() the client on the terminal disconnect (like the
    // done/error/watchdog paths) — otherwise WsClient keeps auto-reconnecting
    // behind the terminal UI. FakeSocket.close() flips `closed`.
    expect(h.get().closed).toBe(true);
  });

  it('treats an open-but-silent stream as terminal error with retry', () => {
    vi.useFakeTimers();
    const h = harness();
    const { result } = renderHook(() =>
      useChatStream({ streamId: SID, makeClient: h.makeClient, idleTimeoutMs: 10 }),
    );

    act(() => {
      vi.advanceTimersByTime(9);
    });
    expect(result.current.phase).toBe('idle');

    act(() => {
      vi.advanceTimersByTime(1);
    });

    expect(result.current.phase).toBe('error');
    expect(result.current.problem?.code).toBe('stream_disconnected');
  });

  it('treats a start-then-silence stall (no first token) as terminal error with retry (#159)', () => {
    vi.useFakeTimers();
    const h = harness();
    const { result } = renderHook(() =>
      useChatStream({ streamId: SID, makeClient: h.makeClient, idleTimeoutMs: 10 }),
    );

    // Backend opens the stream and announces `start`, then stalls before the
    // first delta/event/terminal. The `start` must re-arm the idle watchdog.
    act(() => {
      h.get().emit({ type: 'start', streamId: SID, seq: 0, data: {} });
      vi.advanceTimersByTime(9);
    });
    // `start` re-armed the watchdog; it has not yet fired, so no terminal error.
    expect(result.current.phase).not.toBe('error');

    act(() => {
      vi.advanceTimersByTime(1);
    });

    expect(result.current.phase).toBe('error');
    expect(result.current.problem?.code).toBe('stream_disconnected');
    expect(h.get().closed).toBe(true);
  });

  it('treats silence after a side-band event (no terminal) as terminal error (#159)', () => {
    vi.useFakeTimers();
    const h = harness();
    const { result } = renderHook(() =>
      useChatStream({ streamId: SID, makeClient: h.makeClient, idleTimeoutMs: 10 }),
    );

    // start → event (e.g. tool_call) → silence: the `event` must re-arm too.
    act(() => {
      h.get().emit({ type: 'start', streamId: SID, seq: 0, data: {} });
      h.get().emit({ type: 'event', streamId: SID, seq: 1, name: 'tool_call', data: {} });
      vi.advanceTimersByTime(9);
    });
    expect(result.current.phase).not.toBe('error');

    act(() => {
      vi.advanceTimersByTime(1);
    });

    expect(result.current.phase).toBe('error');
    expect(result.current.problem?.code).toBe('stream_disconnected');
  });

  it('treats idle silence between tokens as terminal error while preserving text', () => {
    vi.useFakeTimers();
    const h = harness();
    const { result } = renderHook(() =>
      useChatStream({ streamId: SID, makeClient: h.makeClient, idleTimeoutMs: 10 }),
    );

    act(() => {
      h.get().emit({ type: 'start', streamId: SID, seq: 0, data: {} });
      h.get().emit({ type: 'delta', streamId: SID, seq: 1, data: { text: 'half' } });
      vi.advanceTimersByTime(9);
    });
    expect(result.current.phase).toBe('streaming');

    act(() => {
      vi.advanceTimersByTime(1);
    });

    expect(result.current.phase).toBe('error');
    expect(result.current.problem?.code).toBe('stream_disconnected');
    expect(result.current.text).toBe('half');
  });

  it('does NOT flag disconnect-as-error after a clean done', () => {
    const h = harness();
    const { result } = renderHook(() => useChatStream({ streamId: SID, makeClient: h.makeClient }));
    act(() => {
      h.get().emit({ type: 'start', streamId: SID, seq: 0, data: {} });
      h.get().emit({
        type: 'done',
        streamId: SID,
        seq: 1,
        data: { messageId: 'm', finishReason: 'stop', citationCount: 0 },
      });
    });
    act(() => h.get().drop());
    expect(result.current.phase).toBe('done');
  });

  it('closes the socket after clean done so completed streams do not reconnect', async () => {
    const h = harness();
    const { result } = renderHook(() => useChatStream({ streamId: SID, makeClient: h.makeClient }));
    act(() => {
      h.get().emit({ type: 'start', streamId: SID, seq: 0, data: {} });
      h.get().emit({
        type: 'done',
        streamId: SID,
        seq: 1,
        data: { messageId: 'm', finishReason: 'stop', citationCount: 0 },
      });
    });

    await waitFor(() => expect(result.current.phase).toBe('done'));
    expect(h.get().closed).toBe(true);
  });

  it('cancel() closes the socket', () => {
    const h = harness();
    const { result } = renderHook(() => useChatStream({ streamId: SID, makeClient: h.makeClient }));
    act(() => result.current.cancel());
    expect(h.get().closed).toBe(true);
  });

  it('opens the socket at /chat/<streamId>', () => {
    const h = harness();
    renderHook(() => useChatStream({ streamId: SID, makeClient: h.makeClient }));
    expect(h.get().opts.path).toBe(`/chat/${SID}`);
  });

  // --- #489: post-terminal suggestions window --------------------------------

  it('holds the socket open on a pendingSuggestions done and accepts the trailing suggestions (AC-2)', async () => {
    const h = harness();
    const onDone = vi.fn();
    const { result } = renderHook(() =>
      useChatStream({ streamId: SID, makeClient: h.makeClient, onDone, suggestionsGraceMs: 5_000 }),
    );
    act(() => {
      h.get().emit({ type: 'start', streamId: SID, seq: 0, data: {} });
      h.get().emit({ type: 'delta', streamId: SID, seq: 1, data: { text: 'Answer.' } });
      h.get().emit({
        type: 'done',
        streamId: SID,
        seq: 2,
        data: { messageId: 'm', finishReason: 'stop', citationCount: 0, pendingSuggestions: true },
      });
    });
    // Terminal settled + onDone fired, but the socket is STILL OPEN for the
    // post-terminal suggestions (unlike a plain done, which closes at once).
    expect(result.current.phase).toBe('done');
    expect(onDone).toHaveBeenCalledTimes(1);
    expect(h.get().closed).toBe(false);

    // The suggestions event arrives AFTER the stream closed to the reducer.
    act(() => {
      h.get().emit({
        type: 'event',
        streamId: SID,
        seq: 3,
        name: 'suggestions',
        data: { messageId: 'm', suggestions: ['What next?', 'Who owns it?'] },
      });
    });
    expect(result.current.phase).toBe('done'); // still settled — not un-terminaled
    expect(result.current.suggestions).toEqual({
      messageId: 'm',
      suggestions: ['What next?', 'Who owns it?'],
    });
    // Got what we waited for → socket closed, no disconnect error synthesized.
    expect(h.get().closed).toBe(true);
    expect(result.current.problem).toBeNull();
  });

  it('closes the socket at grace expiry when no suggestions follow a pendingSuggestions done', () => {
    vi.useFakeTimers();
    const h = harness();
    const { result } = renderHook(() =>
      useChatStream({ streamId: SID, makeClient: h.makeClient, suggestionsGraceMs: 5_000 }),
    );
    act(() => {
      h.get().emit({ type: 'start', streamId: SID, seq: 0, data: {} });
      h.get().emit({
        type: 'done',
        streamId: SID,
        seq: 1,
        data: { messageId: 'm', finishReason: 'stop', citationCount: 0, pendingSuggestions: true },
      });
    });
    expect(h.get().closed).toBe(false); // held open during the grace
    act(() => vi.advanceTimersByTime(5_000));
    // Grace expired: socket closed, terminal stands, no disconnect error.
    expect(h.get().closed).toBe(true);
    expect(result.current.phase).toBe('done');
    expect(result.current.problem).toBeNull();
  });

  it('honours a server suggestionsGraceMs larger than the client default (BE-5)', () => {
    vi.useFakeTimers();
    const h = harness();
    // No explicit suggestionsGraceMs prop → the hook's own 15s default would apply
    // if the server declared none. The server declares 25s (> 15s), which must win:
    // a valid config where the server grace outlasts the old hard-coded client
    // default would otherwise cut off the live suggestion at 15s.
    const { result } = renderHook(() => useChatStream({ streamId: SID, makeClient: h.makeClient }));
    act(() => {
      h.get().emit({ type: 'start', streamId: SID, seq: 0, data: {} });
      h.get().emit({
        type: 'done',
        streamId: SID,
        seq: 1,
        data: {
          messageId: 'm',
          finishReason: 'stop',
          citationCount: 0,
          pendingSuggestions: true,
          suggestionsGraceMs: 25_000,
        },
      });
    });
    // Past the OLD hard-coded 15s default: STILL open, because the server grace is 25s.
    act(() => vi.advanceTimersByTime(15_000));
    expect(h.get().closed).toBe(false);
    // Only at the server-declared grace (25s) does the client give up and close.
    act(() => vi.advanceTimersByTime(10_000));
    expect(h.get().closed).toBe(true);
    expect(result.current.phase).toBe('done');
    expect(result.current.problem).toBeNull();
  });
});

// --- #493: batch stream deltas before committing to React state --------------
// Text deltas are accumulated at the hook ingress and flushed once per animation
// frame (rAF is faked by vi.useFakeTimers, so flushes are driven deterministically
// by advancing timers — never real waiting). Non-text envelopes flush the buffer
// first; teardown cancels any pending flush.
describe('useChatStream — delta batching (#493)', () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it('coalesces N text deltas into a bounded number of commits (AC-1)', () => {
    vi.useFakeTimers();
    const h = harness();
    let renders = 0;
    const { result } = renderHook(() => {
      renders++;
      return useChatStream({ streamId: SID, makeClient: h.makeClient });
    });
    const baseline = renders;

    // Each WS frame arrives in its OWN task (a separate `onmessage`), so React's
    // automatic batching cannot coalesce them — model that by flushing React
    // between emits (a fresh `act` per delta). Without ingress batching this is
    // one commit PER delta; with it, the deltas buffer and commit once per frame.
    act(() => {
      h.get().emit({ type: 'start', streamId: SID, seq: 0, data: {} });
    });
    for (let i = 1; i <= 200; i++) {
      act(() => {
        h.get().emit({ type: 'delta', streamId: SID, seq: i, data: { text: 'x' } });
      });
    }

    // The 200 separately-delivered deltas have committed only a handful of times
    // (bounded by flush cadence, not by N) — they are buffered, not dispatched.
    const committedByDeltas = renders - baseline;
    expect(committedByDeltas).toBeLessThan(20);

    // Drive the coalesced flush (one animation frame).
    act(() => {
      vi.advanceTimersByTime(50);
    });

    expect(result.current.text).toBe('x'.repeat(200));
    // Total commits stay an order of magnitude below the delta count.
    expect(renders - baseline).toBeLessThan(20);
  });

  it('final text equals the concatenation of all deltas, in order (AC-2)', () => {
    vi.useFakeTimers();
    const h = harness();
    const { result } = renderHook(() => useChatStream({ streamId: SID, makeClient: h.makeClient }));

    const tokens = Array.from({ length: 60 }, (_, i) => `${i}.`);
    act(() => {
      h.get().emit({ type: 'start', streamId: SID, seq: 0, data: {} });
      tokens.forEach((text, i) => {
        h.get().emit({ type: 'delta', streamId: SID, seq: i + 1, data: { text } });
      });
      vi.advanceTimersByTime(50);
    });

    expect(result.current.text).toBe(tokens.join(''));
  });

  it('flushes the pending buffer before a terminal done arriving right after a delta (AC-3)', () => {
    // Fake timers ensure the animation-frame flush does NOT fire on its own —
    // the terminal `done` must flush the buffer itself.
    vi.useFakeTimers();
    const h = harness();
    const { result } = renderHook(() => useChatStream({ streamId: SID, makeClient: h.makeClient }));

    act(() => {
      h.get().emit({ type: 'start', streamId: SID, seq: 0, data: {} });
      h.get().emit({ type: 'delta', streamId: SID, seq: 1, data: { text: 'Answer ' } });
      h.get().emit({ type: 'delta', streamId: SID, seq: 2, data: { text: 'complete.' } });
      h.get().emit({
        type: 'done',
        streamId: SID,
        seq: 3,
        data: { messageId: 'm', finishReason: 'stop', citationCount: 0 },
      });
    });

    expect(result.current.phase).toBe('done');
    expect(result.current.text).toBe('Answer complete.');
  });

  it('flushes the pending buffer before a terminal error, preserving partial text (AC-3)', () => {
    vi.useFakeTimers();
    const h = harness();
    const { result } = renderHook(() => useChatStream({ streamId: SID, makeClient: h.makeClient }));

    act(() => {
      h.get().emit({ type: 'start', streamId: SID, seq: 0, data: {} });
      h.get().emit({ type: 'delta', streamId: SID, seq: 1, data: { text: 'partial' } });
      h.get().emit({
        type: 'error',
        streamId: SID,
        seq: 2,
        problem: { title: 'boom', status: 500, code: 'x' },
      });
    });

    expect(result.current.phase).toBe('error');
    expect(result.current.text).toBe('partial');
  });

  it('flushes the pending buffer before a side-band event, preserving order (AC-3)', () => {
    vi.useFakeTimers();
    const h = harness();
    const { result } = renderHook(() => useChatStream({ streamId: SID, makeClient: h.makeClient }));

    act(() => {
      h.get().emit({ type: 'start', streamId: SID, seq: 0, data: {} });
      h.get().emit({ type: 'delta', streamId: SID, seq: 1, data: { text: 'before ' } });
      // A citation event arrives before the frame fires: it must flush the
      // buffered text first, so the text is already present alongside it.
      h.get().emit({
        type: 'event',
        streamId: SID,
        seq: 2,
        name: 'citation',
        data: {
          id: 'c1',
          documentId: 'd1',
          documentName: 'f.pdf',
          chunkId: 'k1',
          snippet: 's',
          charStart: 0,
          charEnd: 5,
        },
      });
      h.get().emit({ type: 'delta', streamId: SID, seq: 3, data: { text: 'after' } });
      vi.advanceTimersByTime(50);
    });

    expect(result.current.text).toBe('before after');
    expect(result.current.citations).toHaveLength(1);
  });

  it('cancels the pending flush on unmount so nothing fires after teardown (AC-4)', () => {
    vi.useFakeTimers();
    const h = harness();
    const { unmount } = renderHook(() =>
      useChatStream({ streamId: SID, makeClient: h.makeClient }),
    );

    act(() => {
      h.get().emit({ type: 'start', streamId: SID, seq: 0, data: {} });
      h.get().emit({ type: 'delta', streamId: SID, seq: 1, data: { text: 'buffered' } });
    });
    // A flush frame is pending (buffer not yet committed).
    expect(vi.getTimerCount()).toBeGreaterThan(0);

    unmount();

    // Cleanup cancelled the pending flush AND the watchdog — no timer may fire
    // after teardown, so no state update lands on an unmounted component.
    expect(vi.getTimerCount()).toBe(0);
    // Advancing anyway is a no-op (nothing scheduled).
    act(() => {
      vi.advanceTimersByTime(1000);
    });
    expect(vi.getTimerCount()).toBe(0);
  });

  it('cancels the pending flush when streamId changes mid-buffer (AC-4)', () => {
    vi.useFakeTimers();
    const h = harness();
    const { result, rerender } = renderHook(
      (props: { streamId: string }) => useChatStream({ ...props, makeClient: h.makeClient }),
      { initialProps: { streamId: SID } },
    );

    act(() => {
      h.get().emit({ type: 'start', streamId: SID, seq: 0, data: {} });
      h.get().emit({ type: 'delta', streamId: SID, seq: 1, data: { text: 'stale' } });
    });
    expect(vi.getTimerCount()).toBeGreaterThan(0);

    // Switching streams tears down the old socket; its buffered text must be
    // discarded, not flushed into the new stream's fresh (reset) state.
    act(() => {
      rerender({ streamId: 'stream-other' });
    });
    act(() => {
      vi.advanceTimersByTime(1000);
    });
    // The new stream started idle with empty text — the old buffered "stale"
    // token never flushed into it.
    expect(result.current.text).toBe('');
  });

  it('discards buffered speculative text on answer_retract (#488) so it does not survive', () => {
    vi.useFakeTimers();
    const h = harness();
    const { result } = renderHook(() => useChatStream({ streamId: SID, makeClient: h.makeClient }));

    act(() => {
      h.get().emit({ type: 'start', streamId: SID, seq: 0, data: {} });
      h.get().emit({ type: 'delta', streamId: SID, seq: 1, data: { text: 'speculative ' } });
      h.get().emit({ type: 'delta', streamId: SID, seq: 2, data: { text: 'answer' } });
      // Whole-turn retraction before the frame fires: the buffered speculative
      // text must be cleared as part of the same commit, never landing after.
      h.get().emit({ type: 'event', streamId: SID, seq: 3, name: 'answer_retract', data: {} });
    });
    expect(result.current.text).toBe('');

    // The real answer then streams normally on top of the cleared text.
    act(() => {
      h.get().emit({ type: 'delta', streamId: SID, seq: 4, data: { text: 'real answer' } });
      vi.advanceTimersByTime(50);
    });
    expect(result.current.text).toBe('real answer');
  });
});

// --- FE-4: socket callbacks must not survive teardown ------------------------
// A real WebSocket.close() reports `closed` on a LATER tick, so the socket that a
// finished effect run created can still fire onStateChange/onEnvelope AFTER that
// run is torn down — on unmount, or on a streamId change that has already reset
// the shared refs for the NEXT stream. Each effect run carries a liveness token;
// a stale callback must observe it and no-op (no setConnection after unmount, no
// bleed into the new stream's terminalRef / clientRef / delta buffer).
describe('useChatStream — teardown liveness (FE-4)', () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  /** Like FakeSocket, but close() reports `closed` on a later tick (real WS). */
  class AsyncCloseSocket implements StreamSocket {
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
      // A real WebSocket's 'close' event lands asynchronously, not inline.
      setTimeout(() => this.opts.onStateChange?.('closed'), 0);
    }
    getState() {
      return this.closed ? ('closed' as const) : ('open' as const);
    }
    emit(envelope: WsEnvelope): void {
      this.opts.onEnvelope?.(envelope);
    }
  }

  function multiHarness() {
    const sockets: AsyncCloseSocket[] = [];
    const makeClient: ClientFactory = (opts) => {
      const s = new AsyncCloseSocket(opts);
      sockets.push(s);
      return s;
    };
    return { makeClient, sockets };
  }

  /** The nth constructed socket — throws (rather than silently no-op) if absent. */
  function socketAt(sockets: AsyncCloseSocket[], index: number): AsyncCloseSocket {
    const socket = sockets[index];
    if (!socket) throw new Error(`expected a socket at index ${index}`);
    return socket;
  }

  it('a stale async close from the previous streamId does not bleed into the new stream', () => {
    vi.useFakeTimers();
    const h = multiHarness();
    const { result, rerender } = renderHook(
      (props: { streamId: string }) =>
        useChatStream({ ...props, makeClient: h.makeClient, idleTimeoutMs: 50_000 }),
      { initialProps: { streamId: SID } },
    );

    // Stream A is mid-flight (start + a partial delta buffered).
    act(() => {
      socketAt(h.sockets, 0).emit({ type: 'start', streamId: SID, seq: 0, data: {} });
      socketAt(h.sockets, 0).emit({
        type: 'delta',
        streamId: SID,
        seq: 1,
        data: { text: 'A-partial' },
      });
    });

    // Switch streams: cleanup closes socket A, but A only reports `closed` on a
    // LATER tick. The new effect run resets the shared refs and opens socket B.
    act(() => {
      rerender({ streamId: 'stream-B' });
    });
    act(() => {
      socketAt(h.sockets, 1).emit({ type: 'start', streamId: 'stream-B', seq: 0, data: {} });
    });
    expect(result.current.phase).toBe('streaming');
    expect(result.current.connection).toBe('open');

    // NOW socket A's deferred `closed` fires. It must be inert against stream B:
    // no synthesized disconnect, no connection flip, no close of B's socket.
    act(() => {
      vi.advanceTimersByTime(1);
    });
    expect(result.current.phase).toBe('streaming');
    expect(result.current.connection).toBe('open');
    expect(socketAt(h.sockets, 1).closed).toBe(false);
    expect(result.current.problem).toBeNull();
  });

  it('a deferred close after unmount performs no state update (no act warning)', () => {
    vi.useFakeTimers();
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    const h = multiHarness();
    const { unmount } = renderHook(() =>
      useChatStream({ streamId: SID, makeClient: h.makeClient, idleTimeoutMs: 50_000 }),
    );
    act(() => {
      socketAt(h.sockets, 0).emit({ type: 'start', streamId: SID, seq: 0, data: {} });
      socketAt(h.sockets, 0).emit({
        type: 'delta',
        streamId: SID,
        seq: 1,
        data: { text: 'partial' },
      });
    });

    unmount();
    // The socket's deferred `closed` lands after teardown; the stale callback
    // must no-op — no setConnection on the unmounted hook, hence no act warning.
    act(() => {
      vi.advanceTimersByTime(1);
    });
    expect(errorSpy).not.toHaveBeenCalled();
    errorSpy.mockRestore();
  });
});

// --- FE-5: envelopes arriving after the stream is terminal -------------------
// Once the stream has settled (done/error/cancel/disconnect), a queued or stray
// `delta` must NOT re-arm the idle watchdog or schedule a flush, and every close
// path must leave no timer alive. The one exception is the single post-terminal
// `event:suggestions` we hold the socket open for (#489), still honoured.
describe('useChatStream — post-terminal envelopes (FE-5)', () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it('ignores a late delta after done: no watchdog re-arm, no scheduled flush, no surviving timer', () => {
    vi.useFakeTimers();
    const h = harness();
    const { result } = renderHook(() =>
      useChatStream({ streamId: SID, makeClient: h.makeClient, idleTimeoutMs: 10_000 }),
    );

    act(() => {
      h.get().emit({ type: 'start', streamId: SID, seq: 0, data: {} });
      h.get().emit({ type: 'delta', streamId: SID, seq: 1, data: { text: 'Answer.' } });
      h.get().emit({
        type: 'done',
        streamId: SID,
        seq: 2,
        data: { messageId: 'm', finishReason: 'stop', citationCount: 0 },
      });
    });
    expect(result.current.phase).toBe('done');
    expect(result.current.text).toBe('Answer.');
    // Clean done closed the socket and cleared EVERY timer (watchdog + flush).
    expect(vi.getTimerCount()).toBe(0);

    // A straggler delta after the terminal must be ignored entirely: it must not
    // buffer text, schedule a flush frame, or re-arm the idle watchdog.
    act(() => {
      h.get().emit({ type: 'delta', streamId: SID, seq: 3, data: { text: ' LATE' } });
    });
    expect(vi.getTimerCount()).toBe(0);

    // Advancing past the idle timeout synthesizes no disconnect and the stray
    // text never lands — the terminal `done` stands.
    act(() => {
      vi.advanceTimersByTime(20_000);
    });
    expect(result.current.phase).toBe('done');
    expect(result.current.text).toBe('Answer.');
    expect(result.current.problem).toBeNull();
  });

  it('ignores a late delta after a terminal error and leaves no timer alive', () => {
    vi.useFakeTimers();
    const h = harness();
    const { result } = renderHook(() =>
      useChatStream({ streamId: SID, makeClient: h.makeClient, idleTimeoutMs: 10_000 }),
    );

    act(() => {
      h.get().emit({ type: 'start', streamId: SID, seq: 0, data: {} });
      h.get().emit({
        type: 'error',
        streamId: SID,
        seq: 1,
        problem: { title: 'boom', status: 500, code: 'x' },
      });
    });
    expect(result.current.phase).toBe('error');
    expect(vi.getTimerCount()).toBe(0);

    act(() => {
      h.get().emit({ type: 'delta', streamId: SID, seq: 2, data: { text: 'stray' } });
    });
    expect(vi.getTimerCount()).toBe(0);
    act(() => {
      vi.advanceTimersByTime(20_000);
    });
    expect(result.current.phase).toBe('error');
  });

  it('ignores a late delta after cancel() so nothing re-arms', () => {
    vi.useFakeTimers();
    const h = harness();
    const { result } = renderHook(() =>
      useChatStream({ streamId: SID, makeClient: h.makeClient, idleTimeoutMs: 10_000 }),
    );

    act(() => {
      h.get().emit({ type: 'start', streamId: SID, seq: 0, data: {} });
      h.get().emit({ type: 'delta', streamId: SID, seq: 1, data: { text: 'partial' } });
    });
    act(() => {
      result.current.cancel();
    });
    // cancel() cleared the watchdog and the pending flush.
    expect(vi.getTimerCount()).toBe(0);

    act(() => {
      h.get().emit({ type: 'delta', streamId: SID, seq: 2, data: { text: ' more' } });
    });
    // No re-arm, no scheduled flush from a post-cancel straggler.
    expect(vi.getTimerCount()).toBe(0);
  });

  it('still accepts the one post-terminal suggestions event within the grace (#489 preserved)', () => {
    vi.useFakeTimers();
    const h = harness();
    const { result } = renderHook(() =>
      useChatStream({ streamId: SID, makeClient: h.makeClient, suggestionsGraceMs: 5_000 }),
    );
    act(() => {
      h.get().emit({ type: 'start', streamId: SID, seq: 0, data: {} });
      h.get().emit({ type: 'delta', streamId: SID, seq: 1, data: { text: 'Answer.' } });
      h.get().emit({
        type: 'done',
        streamId: SID,
        seq: 2,
        data: { messageId: 'm', finishReason: 'stop', citationCount: 0, pendingSuggestions: true },
      });
    });
    expect(result.current.phase).toBe('done');
    expect(h.get().closed).toBe(false); // held open for the trailing suggestions

    act(() => {
      h.get().emit({
        type: 'event',
        streamId: SID,
        seq: 3,
        name: 'suggestions',
        data: { messageId: 'm', suggestions: ['Next?'] },
      });
    });
    // The awaited suggestions is applied (not ignored) and the socket closes.
    expect(result.current.suggestions).toEqual({ messageId: 'm', suggestions: ['Next?'] });
    expect(h.get().closed).toBe(true);
    expect(vi.getTimerCount()).toBe(0);
    expect(result.current.problem).toBeNull();
  });
});
