/**
 * ChatView integration (the critical flow, frontend/AGENTS.md + ADR-0006 Phase 1):
 * pick a session → send a message (POST .../messages) → subscribe to the returned
 * stream_id over an INJECTED fake WS → render delta tokens + tool activity + a
 * clickable citation live → on `done`, reload messages from GET .../messages.
 * Also: a stream error is terminal with Retry (AC-5); a zero-citation answer is
 * shown honestly.
 *
 * Server calls go through the real api/ client against a mocked fetch; the WS is
 * a controllable fake so the test is deterministic and network-free.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { act, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderWithQuery } from '@/test/renderWithQuery';
import { TooltipProvider } from '@/components/Tooltip';
import { setAccessToken, clearAccessToken } from '@/api';
import type { WsClientOptions, WsEnvelope } from '@/api';
import { ChatView } from './ChatView';
import { useChatStore } from '../model/chatStore';
import {
  setDefaultStreamClientFactory,
  type StreamSocket,
} from '../model/useChatStream';

// --- a controllable fake WS socket (captures the latest instance) ---
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

const MODELS = {
  items: [
    { id: 'frontier/opus', label: 'Opus', provider: 'anthropic', tier: 'frontier', is_default: true },
    { id: 'fast/haiku', label: 'Haiku', provider: 'anthropic', tier: 'fast', is_default: false },
  ],
};
const SESSIONS = {
  items: [
    {
      id: 'sess-1',
      title: 'My chat',
      model: 'frontier/opus',
      owner_id: 'u1',
      message_count: 0,
      created_at: '2026-06-18T00:00:00Z',
      updated_at: '2026-06-18T00:00:00Z',
    },
  ],
  next_cursor: null,
};
const EMPTY_MESSAGES = { items: [], next_cursor: null };

const userMsg = {
  id: 'um-1',
  session_id: 'sess-1',
  role: 'user',
  content: 'What was Q4 revenue?',
  citations: [],
  created_at: '2026-06-18T00:01:00Z',
};
const sendResponse = { message: userMsg, stream_id: 'stream-1' };

// Messages after the stream completes (the authoritative reload — AC-2).
const RELOADED_MESSAGES = {
  items: [
    userMsg,
    {
      id: 'am-1',
      session_id: 'sess-1',
      role: 'assistant',
      content: 'Revenue was up 12%.',
      model: 'frontier/opus',
      citations: [
        {
          id: 'cite-1',
          document_id: 'doc-1',
          document_name: 'Q4 report.pdf',
          chunk_id: 'k1',
          snippet: 'Revenue grew 12% year over year.',
          char_start: 50,
          char_end: 90,
        },
      ],
      created_at: '2026-06-18T00:01:05Z',
    },
  ],
  next_cursor: null,
};

/** Route a mocked fetch by URL + method. `messages` flips after the stream done. */
function installFetch(getMessages: () => unknown) {
  return vi.spyOn(globalThis, 'fetch').mockImplementation((input, init) => {
    const url = typeof input === 'string' ? input : (input as Request).url ?? String(input);
    const method = (init?.method ?? 'GET').toUpperCase();
    if (url.includes('/models')) return Promise.resolve(json(MODELS));
    if (url.endsWith('/chat/sessions') && method === 'GET') return Promise.resolve(json(SESSIONS));
    if (url.includes('/messages') && method === 'POST')
      return Promise.resolve(json(sendResponse, 202));
    if (url.includes('/messages') && method === 'GET')
      return Promise.resolve(json(getMessages()));
    if (url.includes('/chat/sessions/') && method === 'PATCH')
      return Promise.resolve(json({ ...SESSIONS.items[0] }));
    return Promise.resolve(json({ items: [], next_cursor: null }));
  });
}

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
  liveSocket = null;
  restoreFactory = setDefaultStreamClientFactory((opts) => {
    liveSocket = new FakeSocket(opts);
    return liveSocket;
  });
});
afterEach(() => {
  restoreFactory();
  clearAccessToken();
  vi.restoreAllMocks();
});

function renderView() {
  return renderWithQuery(
    <TooltipProvider>
      <ChatView />
    </TooltipProvider>,
  );
}

describe('ChatView (critical flow)', () => {
  it('sends → streams delta/tool/citation → done reloads with a clickable citation (AC-1/2)', async () => {
    let messagesState: unknown = EMPTY_MESSAGES;
    installFetch(() => messagesState);
    const user = userEvent.setup();
    renderView();

    // Open the session from the sidebar.
    await user.click(await screen.findByRole('button', { name: 'My chat' }));
    // The empty conversation prompt shows before any message.
    expect(await screen.findByText(/ask a question to start/i)).toBeInTheDocument();

    // Send a message.
    await user.type(screen.getByLabelText('Message'), 'What was Q4 revenue?');
    await user.click(screen.getByRole('button', { name: /send/i }));

    // The user turn appears (optimistic), and the WS opens at /chat/<stream_id>.
    await screen.findByText('What was Q4 revenue?');
    await waitFor(() => expect(liveSocket).not.toBeNull());
    expect(liveSocket?.opts.path).toBe('/chat/stream-1');

    // Stream: start → tool activity → delta tokens → citation.
    act(() => {
      liveSocket!.emit({
        type: 'start',
        streamId: 'stream-1',
        seq: 0,
        data: { sessionId: 'sess-1', messageId: 'am-1', model: 'frontier/opus' },
      });
      liveSocket!.emit({
        type: 'event',
        streamId: 'stream-1',
        seq: 1,
        name: 'tool_call',
        data: { callId: 't1', tool: 'search_text', args: {} },
      });
    });
    expect(screen.getByText(/searching documents/i)).toBeInTheDocument();

    act(() => {
      liveSocket!.emit({
        type: 'event',
        streamId: 'stream-1',
        seq: 2,
        name: 'tool_result',
        data: { callId: 't1', tool: 'search_text', hitCount: 2 },
      });
      liveSocket!.emit({ type: 'delta', streamId: 'stream-1', seq: 3, data: { text: 'Revenue ' } });
      liveSocket!.emit({ type: 'delta', streamId: 'stream-1', seq: 4, data: { text: 'was up 12%.' } });
      liveSocket!.emit({
        type: 'event',
        streamId: 'stream-1',
        seq: 5,
        name: 'citation',
        data: {
          id: 'cite-1',
          documentId: 'doc-1',
          documentName: 'Q4 report.pdf',
          chunkId: 'k1',
          snippet: 'Revenue grew 12% year over year.',
          charStart: 50,
          charEnd: 90,
        },
      });
    });
    expect(screen.getByText(/Revenue was up 12%\./)).toBeInTheDocument();

    // Done → reload from the server (now returns the persisted assistant turn).
    messagesState = RELOADED_MESSAGES;
    act(() => {
      liveSocket!.emit({
        type: 'done',
        streamId: 'stream-1',
        seq: 6,
        data: { messageId: 'am-1', finishReason: 'stop', citationCount: 1 },
      });
    });

    // Let the server reload settle (the live streaming turn is retired once the
    // persisted assistant message arrives) before interacting with the citation.
    await waitFor(() => expect(useChatStore.getState().activeStreamId).toBeNull());

    // The reloaded assistant message renders a clickable citation chip; clicking
    // opens the viewer pane at the cited document (AC-2 click-through).
    const citeButton = await screen.findByRole('button', {
      name: /citation 1: Q4 report\.pdf/i,
    });
    await user.click(citeButton);
    const viewer = await screen.findByRole('region', { name: /cited document: Q4 report\.pdf/i });
    expect(within(viewer).getByText(/characters 50–90/i)).toBeInTheDocument();
  });

  it('treats a WS error as terminal with a Retry (AC-5)', async () => {
    installFetch(() => EMPTY_MESSAGES);
    const user = userEvent.setup();
    renderView();
    await user.click(await screen.findByRole('button', { name: 'My chat' }));
    await user.type(screen.getByLabelText('Message'), 'hi');
    await user.click(screen.getByRole('button', { name: /send/i }));
    await waitFor(() => expect(liveSocket).not.toBeNull());

    act(() => {
      liveSocket!.emit({ type: 'start', streamId: 'stream-1', seq: 0, data: {} });
      liveSocket!.emit({
        type: 'error',
        streamId: 'stream-1',
        seq: 1,
        problem: { title: 'Upstream failed', status: 502, detail: 'model unavailable' },
      });
    });

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(/model unavailable/i);
    expect(within(alert).getByRole('button', { name: /retry/i })).toBeInTheDocument();
  });

  it('keeps chat history reachable at narrow widths via the drawer (AC-4, #136)', async () => {
    installFetch(() => EMPTY_MESSAGES);
    const user = userEvent.setup();
    renderView();
    // The history subrail is an off-canvas drawer at narrow widths; it starts
    // closed and is opened from the in-pane "Chats" trigger (so a phone user can
    // still list / create / switch sessions — not stranded on the empty state).
    const subrail = document.querySelector('.lc-chat__subrail');
    expect(subrail).toHaveAttribute('data-open', 'false');
    await user.click(screen.getByRole('button', { name: /open chat history/i }));
    expect(subrail).toHaveAttribute('data-open', 'true');
    // The tap-away backdrop closes it again.
    await user.click(screen.getByRole('button', { name: /close chat history/i }));
    expect(subrail).toHaveAttribute('data-open', 'false');
    // Opening a session from the drawer also closes it.
    await user.click(screen.getByRole('button', { name: /open chat history/i }));
    await user.click(await screen.findByRole('button', { name: 'My chat' }));
    expect(subrail).toHaveAttribute('data-open', 'false');
  });

  it('shows a zero-citation answer honestly (AC-5)', async () => {
    installFetch(() => EMPTY_MESSAGES);
    const user = userEvent.setup();
    renderView();
    await user.click(await screen.findByRole('button', { name: 'My chat' }));
    await user.type(screen.getByLabelText('Message'), 'unknowable?');
    await user.click(screen.getByRole('button', { name: /send/i }));
    await waitFor(() => expect(liveSocket).not.toBeNull());

    act(() => {
      liveSocket!.emit({ type: 'start', streamId: 'stream-1', seq: 0, data: {} });
      liveSocket!.emit({
        type: 'delta',
        streamId: 'stream-1',
        seq: 1,
        data: { text: 'I could not find that in your documents.' },
      });
      liveSocket!.emit({
        type: 'done',
        streamId: 'stream-1',
        seq: 2,
        data: { messageId: 'am-x', finishReason: 'stop', citationCount: 0 },
      });
    });

    expect(await screen.findByText(/no sources were cited/i)).toBeInTheDocument();
  });

  it('shows Web mode DISABLED with a reason for an ad-hoc chat (#221 AC-3)', async () => {
    installFetch(() => EMPTY_MESSAGES);
    const user = userEvent.setup();
    renderView();
    // Opening from the sidebar seeds NO scope → web fails closed.
    await user.click(await screen.findByRole('button', { name: 'My chat' }));
    // Wait for models so the control is enabled — then Web is disabled by
    // AVAILABILITY (not by the loading state), with its reason.
    await waitFor(() => expect(screen.getByLabelText('Message')).not.toBeDisabled());
    const web = await screen.findByRole('button', { name: 'Web' });
    expect(web).toBeDisabled();
    expect(web.getAttribute('title')).toMatch(/not enabled|off/i);
  });

  it('reflects a web-enabled assistant scope and renders a web citation + disclosure (#221 AC-1/AC-2)', async () => {
    installFetch(() => EMPTY_MESSAGES);
    const user = userEvent.setup();
    renderView();

    // Simulate launching this session FROM an assistant whose scope includes web
    // (AssistantLibrary calls openSession(id, modes)); the composer must reflect it.
    act(() => {
      useChatStore.getState().openSession('sess-1', ['company', 'web']);
    });

    // Wait for models to load so the composer (and its mode control) is enabled.
    await waitFor(() => expect(screen.getByLabelText('Message')).not.toBeDisabled());
    const web = await screen.findByRole('button', { name: 'Web' });
    expect(web).not.toBeDisabled();
    expect(web).toHaveAttribute('aria-pressed', 'true');

    // Send a message and stream a web-search tool + a web citation.
    await user.type(screen.getByLabelText('Message'), 'latest AI regulations?');
    await user.click(screen.getByRole('button', { name: /send/i }));
    await waitFor(() => expect(liveSocket).not.toBeNull());

    act(() => {
      liveSocket!.emit({ type: 'start', streamId: 'stream-1', seq: 0, data: {} });
      liveSocket!.emit({
        type: 'event',
        streamId: 'stream-1',
        seq: 1,
        name: 'tool_call',
        data: { callId: 'w1', tool: 'web_search', args: { query: 'AI regulations' } },
      });
    });
    // The web tool activity reads distinctly from document search.
    expect(screen.getByText(/searching the web/i)).toBeInTheDocument();

    act(() => {
      liveSocket!.emit({
        type: 'event',
        streamId: 'stream-1',
        seq: 2,
        name: 'tool_result',
        data: { callId: 'w1', tool: 'web_search', hitCount: 1 },
      });
      liveSocket!.emit({
        type: 'delta',
        streamId: 'stream-1',
        seq: 3,
        data: { text: 'The EU AI Act phases in through 2026.' },
      });
      liveSocket!.emit({
        type: 'event',
        streamId: 'stream-1',
        seq: 4,
        name: 'citation',
        // A web citation: url + no documentId (the additive shape #219 emits).
        data: {
          id: 'web-cite-1',
          documentName: 'EU AI Act overview',
          snippet: 'The Act enters into force in stages through 2026.',
          charStart: 0,
          charEnd: 49,
          url: 'https://www.example.org/eu-ai-act',
          webTitle: 'EU AI Act overview',
        },
      });
      liveSocket!.emit({
        type: 'done',
        streamId: 'stream-1',
        seq: 5,
        data: { messageId: 'am-web', finishReason: 'stop', citationCount: 1 },
      });
    });

    // The web citation renders DISTINCTLY (a "Web sources" block + host), the
    // outbound link is safe, and the "web sources were used" disclosure shows.
    expect(await screen.findByText('Web sources')).toBeInTheDocument();
    expect(screen.getByText('example.org')).toBeInTheDocument();
    const link = screen.getByRole('link', { name: /open .* in a new tab/i });
    expect(link).toHaveAttribute('rel', 'noopener noreferrer');
    expect(link).toHaveAttribute('target', '_blank');
    expect(screen.getByText(/web sources were used/i)).toBeInTheDocument();
  });
});

describe('#434 round-1: suggestions must not survive a terminal error', () => {
  it('suggestions event followed by error leaves no chips and no ghost', async () => {
    installFetch(() => EMPTY_MESSAGES);
    const user = userEvent.setup();
    renderView();
    await user.click(await screen.findByRole('button', { name: 'My chat' }));
    await user.type(screen.getByLabelText('Message'), 'flaky question');
    await user.click(screen.getByRole('button', { name: /send/i }));
    await waitFor(() => expect(liveSocket).not.toBeNull());

    act(() => {
      liveSocket!.emit({
        type: 'start',
        streamId: 'stream-1',
        seq: 0,
        data: { sessionId: 'sess-1', messageId: 'am-1', model: 'frontier/opus' },
      });
      liveSocket!.emit({ type: 'delta', streamId: 'stream-1', seq: 1, data: { text: 'Part.' } });
      // The nicety arrives… then persistence fails and the stream errors.
      liveSocket!.emit({
        type: 'event',
        streamId: 'stream-1',
        seq: 2,
        name: 'suggestions',
        data: { messageId: 'am-1', suggestions: ['Should not appear?'] },
      });
      liveSocket!.emit({
        type: 'error',
        streamId: 'stream-1',
        seq: 3,
        problem: { title: 'Internal Server Error', status: 500 },
      });
    });

    // Terminal error banner shows; no chips, no ghost accept affordance.
    expect(await screen.findByRole('alert')).toBeInTheDocument();
    expect(
      screen.queryByRole('group', { name: 'Suggested follow-up questions' }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: /use suggested question/i }),
    ).not.toBeInTheDocument();
  });
});
