/**
 * useChatStream — subscribes to one answer stream via an INJECTED fake socket
 * (no network). Covers the streaming lifecycle (AC-1), done → onDone (AC-2),
 * terminal error and unexpected-disconnect → terminal (AC-5), and cancel.
 */
import { describe, it, expect, vi } from 'vitest';
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
  it('is idle with no socket when streamId is null', () => {
    const h = harness();
    const { result } = renderHook(() => useChatStream({ streamId: null, makeClient: h.makeClient }));
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
});
