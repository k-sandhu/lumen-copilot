/**
 * #495 AC-1 through the REAL wiring — persisted `MessageBubble` render count
 * stays FLAT across N streamed deltas when the callbacks come from the actual
 * `ActiveSession` (not an injected stub).
 *
 * Why this exists next to `ChatThread.renderHygiene.test.tsx`: that test feeds
 * `ChatThread` a hand-built, deliberately-stable `onSendText`, so it proves the
 * ChatThread half of the invariant but is blind to the half that actually broke
 * it — `ActiveSession` minting a fresh `onSend` per delta. A `useCallback` whose
 * dep array holds a TanStack **mutation result object** (a new wrapper object on
 * every render) does exactly that: `onSend` changes each delta, `PersistedMessages`
 * memo misses, and every historical bubble re-renders. The stub-callback test
 * passes anyway.
 *
 * So this drives the whole `ChatView` — real queries against a mocked fetch, real
 * `useChatStream` against an injected fake socket — with a ≥20-message persisted
 * thread, streams 30 deltas, and asserts ZERO persisted bubble re-renders. It
 * fails loudly if any callback on the persisted render path regains a per-render
 * identity.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { act, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderWithQuery } from '@/test/renderWithQuery';
import { TooltipProvider } from '@/components/Tooltip';
import { setAccessToken, clearAccessToken } from '@/api';
import type { Message, WsClientOptions, WsEnvelope } from '@/api';
import { ChatView } from './ChatView';
import { useChatStore } from '../model/chatStore';
import { setDefaultStreamClientFactory, type StreamSocket } from '../model/useChatStream';

// Record every MessageBubble render by role+content so persisted turns are
// countable and separable from the single live streaming turn.
const { bubbleRenders } = vi.hoisted(() => ({ bubbleRenders: [] as string[] }));
vi.mock('./MessageBubble', () => ({
  MessageBubble: ({ role, content }: { role: string; content: string }) => {
    bubbleRenders.push(`${role}:${content}`);
    return <div data-testid="bubble" data-content={content} />;
  },
}));

let liveSocket: FakeSocket | null = null;
class FakeSocket implements StreamSocket {
  opts: WsClientOptions;
  closed = false;
  constructor(opts: WsClientOptions) {
    this.opts = opts;
  }
  connect() {
    this.opts.onStateChange?.('open');
  }
  close() {
    if (this.closed) return;
    this.closed = true;
    this.opts.onStateChange?.('closed');
  }
  getState() {
    return this.closed ? ('closed' as const) : ('open' as const);
  }
  emit(e: WsEnvelope) {
    this.opts.onEnvelope?.(e);
  }
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

/** Text deltas coalesce to one commit per animation frame (#493) — let it fire. */
async function flushDeltaFrame(): Promise<void> {
  await act(async () => {
    await new Promise<void>((resolve) => {
      requestAnimationFrame(() => resolve());
    });
  });
}

const MODELS = {
  items: [
    {
      id: 'frontier/opus',
      label: 'Opus',
      provider: 'anthropic',
      tier: 'frontier',
      is_default: true,
    },
  ],
};
const SESSIONS = {
  items: [
    {
      id: 'sess-1',
      title: 'My chat',
      model: 'frontier/opus',
      owner_id: 'u1',
      message_count: 24,
      created_at: '2026-06-18T00:00:00Z',
      updated_at: '2026-06-18T00:00:00Z',
    },
  ],
  next_cursor: null,
};

/** A 24-turn persisted thread (12 user + 12 assistant) — comfortably ≥20. */
const PERSISTED: Message[] = Array.from({ length: 24 }, (_, i) => {
  const isAssistant = i % 2 === 1;
  const turn = Math.floor(i / 2);
  return isAssistant
    ? {
        id: `am-${turn}`,
        session_id: 'sess-1',
        role: 'assistant',
        content: `Persisted answer ${turn}`,
        model: 'frontier/opus',
        citations: [],
        tool_invocations: [],
        created_at: '2026-06-18T00:01:00Z',
      }
    : {
        id: `um-${turn}`,
        session_id: 'sess-1',
        role: 'user',
        content: `Persisted question ${turn}`,
        citations: [],
        created_at: '2026-06-18T00:00:59Z',
      };
});
const MESSAGES = { items: PERSISTED, next_cursor: null };

const sendResponse = {
  message: {
    id: 'um-new',
    session_id: 'sess-1',
    role: 'user',
    content: 'And Q4?',
    citations: [],
    created_at: '2026-06-18T00:02:00Z',
  },
  stream_id: 'stream-1',
};

function installFetch() {
  return vi.spyOn(globalThis, 'fetch').mockImplementation((input, init) => {
    const url = typeof input === 'string' ? input : ((input as Request).url ?? String(input));
    const method = (init?.method ?? 'GET').toUpperCase();
    if (url.includes('/models')) return Promise.resolve(json(MODELS));
    if (url.endsWith('/chat/sessions') && method === 'GET') return Promise.resolve(json(SESSIONS));
    if (url.includes('/messages') && method === 'POST')
      return Promise.resolve(json(sendResponse, 202));
    if (url.includes('/messages') && method === 'GET') return Promise.resolve(json(MESSAGES));
    return Promise.resolve(json({ items: [], next_cursor: null }));
  });
}

/** Renders of the persisted thread only (the live turn streams other text). */
const persistedRenders = () => bubbleRenders.filter((r) => r.includes(':Persisted '));

let restoreFactory: () => void;

beforeEach(() => {
  setAccessToken('jwt');
  useChatStore.setState({
    activeSessionId: null,
    activeStreamId: null,
    pendingModel: null,
    viewer: null,
    sessionScope: null,
  });
  bubbleRenders.length = 0;
  liveSocket = null;
  restoreFactory = setDefaultStreamClientFactory((opts) => {
    liveSocket = new FakeSocket(opts);
    return liveSocket;
  });
  installFetch();
});
afterEach(() => {
  restoreFactory();
  clearAccessToken();
  vi.restoreAllMocks();
});

describe('#495 AC-1 through real ActiveSession wiring', () => {
  it('streams 30 deltas over a 24-message thread without re-rendering one persisted bubble', async () => {
    const user = userEvent.setup();
    renderWithQuery(
      <TooltipProvider>
        <ChatView />
      </TooltipProvider>,
    );

    // Open the persisted thread and wait for the whole history to mount.
    await user.click(await screen.findByRole('button', { name: 'My chat' }));
    await waitFor(() => expect(persistedRenders().length).toBeGreaterThanOrEqual(24));
    await waitFor(() => expect(screen.getByLabelText('Message')).not.toBeDisabled());

    // Send a real message through the real mutation → real stream subscription.
    await user.type(screen.getByLabelText('Message'), 'And Q4?');
    await user.click(screen.getByRole('button', { name: /send/i }));
    await waitFor(() => expect(liveSocket).not.toBeNull());
    expect(liveSocket?.opts.path).toBe('/chat/stream-1');

    // Settle the send→stream transition (it legitimately moves `sendBusy` /
    // `hasLive` / the optimistic user turn) before measuring the steady state.
    act(() => {
      liveSocket!.emit({
        type: 'start',
        streamId: 'stream-1',
        seq: 0,
        data: { sessionId: 'sess-1', messageId: 'am-live', model: 'frontier/opus' },
      });
      liveSocket!.emit({ type: 'delta', streamId: 'stream-1', seq: 1, data: { text: 'Live' } });
    });
    await flushDeltaFrame();
    await waitFor(() => expect(bubbleRenders).toContain('assistant:Live'));

    // Baseline: from here the ONLY thing changing is the streaming turn.
    bubbleRenders.length = 0;

    for (let seq = 2; seq < 32; seq++) {
      act(() => {
        liveSocket!.emit({ type: 'delta', streamId: 'stream-1', seq, data: { text: ' tok' } });
      });
      await flushDeltaFrame();
    }

    // The live turn re-rendered on every flush (that IS the turn being written)…
    expect(bubbleRenders.length).toBeGreaterThanOrEqual(30);
    // …and not one persisted bubble moved.
    expect(persistedRenders()).toEqual([]);
  });
});
