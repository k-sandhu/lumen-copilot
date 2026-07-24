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
