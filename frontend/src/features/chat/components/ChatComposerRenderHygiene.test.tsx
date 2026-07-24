/**
 * #495 AC-3 — typing in the composer must not re-render the message list.
 *
 * The composer's draft is local UI state (Composer owns it), so keystrokes must
 * stay inside the composer subtree: they must NOT bubble a re-render up through
 * ActiveSession into ChatThread / the persisted MessageBubbles. This guards the
 * controlled-input boundary directly — render ChatView with a persisted thread,
 * type several characters, and assert the persisted bubbles never re-render.
 *
 * MessageBubble is mocked with a render recorder so the count is exact.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderWithQuery } from '@/test/renderWithQuery';
import { TooltipProvider } from '@/components/Tooltip';
import { setAccessToken, clearAccessToken } from '@/api';
import { ChatView } from './ChatView';
import { useChatStore } from '../model/chatStore';

// Record MessageBubble renders by role+content so persisted turns are countable.
const { bubbleRenders } = vi.hoisted(() => ({ bubbleRenders: [] as string[] }));
vi.mock('./MessageBubble', () => ({
  MessageBubble: ({ role, content }: { role: string; content: string }) => {
    bubbleRenders.push(`${role}:${content}`);
    return <div data-testid="bubble" data-content={content} />;
  },
}));

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
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
      message_count: 2,
      created_at: '2026-06-18T00:00:00Z',
      updated_at: '2026-06-18T00:00:00Z',
    },
  ],
  next_cursor: null,
};
const MESSAGES = {
  items: [
    {
      id: 'um-1',
      session_id: 'sess-1',
      role: 'user',
      content: 'What was Q4 revenue?',
      citations: [],
      created_at: '2026-06-18T00:01:00Z',
    },
    {
      id: 'am-1',
      session_id: 'sess-1',
      role: 'assistant',
      content: 'Revenue was up 12%.',
      model: 'frontier/opus',
      citations: [],
      created_at: '2026-06-18T00:01:05Z',
    },
  ],
  next_cursor: null,
};

function installFetch() {
  return vi.spyOn(globalThis, 'fetch').mockImplementation((input, init) => {
    const url = typeof input === 'string' ? input : ((input as Request).url ?? String(input));
    const method = (init?.method ?? 'GET').toUpperCase();
    if (url.includes('/models')) return Promise.resolve(json(MODELS));
    if (url.endsWith('/chat/sessions') && method === 'GET') return Promise.resolve(json(SESSIONS));
    if (url.includes('/messages') && method === 'GET') return Promise.resolve(json(MESSAGES));
    return Promise.resolve(json({ items: [], next_cursor: null }));
  });
}

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
  installFetch();
});
afterEach(() => {
  clearAccessToken();
  vi.restoreAllMocks();
});

describe('#495 AC-3 composer typing does not re-render the message list', () => {
  it('keystrokes stay inside the composer subtree', async () => {
    const user = userEvent.setup();
    renderWithQuery(
      <TooltipProvider>
        <ChatView />
      </TooltipProvider>,
    );

    // Open the persisted thread and wait for both turns to render.
    await user.click(await screen.findByRole('button', { name: 'My chat' }));
    await waitFor(() =>
      expect(bubbleRenders.some((r) => r.includes('Revenue was up 12%.'))).toBe(true),
    );
    // Composer becomes typable once models load.
    await waitFor(() => expect(screen.getByLabelText('Message')).not.toBeDisabled());

    // Baseline captured; now type into the composer.
    bubbleRenders.length = 0;
    await user.type(screen.getByLabelText('Message'), 'a slowly typed question');

    // Not a single persisted bubble re-rendered on any keystroke.
    expect(bubbleRenders).toHaveLength(0);
    // Sanity: the draft actually landed in the controlled input.
    expect(screen.getByLabelText('Message')).toHaveValue('a slowly typed question');
  });
});
