/**
 * AssistantLibrary (#212) across EVERY async state (frontend/AGENTS.md "every
 * state, not just success"): loading skeleton, empty ("Create your first
 * assistant"), error with retry (401 messaged distinctly), and the success grid.
 * Plus the critical "Start chat" flow (AC-2): clicking it POSTs /chat/sessions
 * with the assistant_id and navigates to the chat workspace (`/`). Rendered
 * against a mocked fetch so a contract match is an integration match (ADR-0006
 * Phase 1).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { screen, within, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Routes, Route, useLocation } from 'react-router-dom';
import type { ReactElement } from 'react';
import { renderWithQuery } from '@/test/renderWithQuery';
import { setAccessToken, clearAccessToken } from '@/api';
import type { Assistant, AssistantList } from '@/api';
import { useChatStore } from '@/features/chat';
import { AssistantLibrary } from './AssistantLibrary';

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}
function problem(status: number, title: string): Response {
  return new Response(JSON.stringify({ type: 'about:blank', title, status }), {
    status,
    headers: { 'Content-Type': 'application/problem+json' },
  });
}

function makeAssistant(overrides: Partial<Assistant> = {}): Assistant {
  return {
    id: 'a1',
    name: 'Benefits helper',
    description: 'Answers HR questions from the handbook',
    instructions: null,
    model: 'anthropic/claude',
    knowledgeScope: { collectionIds: [], sourceIds: [], modes: ['company'] },
    toolAllowlist: ['search_text'],
    autonomyLevel: 'suggest',
    effectiveAutonomy: 'suggest',
    owner: 'u1',
    backupOwner: 'u2',
    status: 'published',
    version: 1,
    created_at: '2026-07-01T00:00:00Z',
    updated_at: '2026-07-01T00:00:00Z',
    ...overrides,
  };
}

const list = (items: Assistant[]): AssistantList => ({ items, next_cursor: null });

/** A location probe so tests can assert navigation to the chat workspace. */
function LocationProbe() {
  const loc = useLocation();
  return <div data-testid="location">{loc.pathname}</div>;
}

function renderLibrary(ui: ReactElement = <AssistantLibrary />) {
  return renderWithQuery(
    <MemoryRouter initialEntries={['/assistants']}>
      <Routes>
        <Route path="/assistants" element={ui} />
        <Route path="/" element={<div>Chat workspace</div>} />
      </Routes>
      <LocationProbe />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  setAccessToken('jwt');
  // Reset the cross-feature chat store so an earlier test's session doesn't leak.
  useChatStore.getState().closeSession();
});
afterEach(() => {
  clearAccessToken();
  vi.restoreAllMocks();
});

describe('AssistantLibrary — states', () => {
  it('renders a LOADING skeleton while the list resolves', async () => {
    let resolve!: (r: Response) => void;
    vi.spyOn(globalThis, 'fetch').mockReturnValue(
      new Promise<Response>((r) => {
        resolve = r;
      }),
    );
    renderLibrary();
    expect(await screen.findByText(/loading assistants/i)).toBeInTheDocument();
    resolve(json(list([makeAssistant()])));
    expect(await screen.findByRole('article', { name: /benefits helper/i })).toBeInTheDocument();
  });

  it('renders the EMPTY state with a primary CTA when there are none', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(json(list([])));
    renderLibrary();
    expect(await screen.findByText(/create your first assistant/i)).toBeInTheDocument();
    // Both the header and the empty-state CTA link to the builder.
    const newLinks = screen.getAllByRole('link', { name: /new assistant/i });
    expect(newLinks.length).toBeGreaterThanOrEqual(1);
    for (const link of newLinks) expect(link).toHaveAttribute('href', '/assistants/new');
  });

  it('renders an actionable ERROR with retry on a transient failure', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(problem(500, 'Server Error'));
    renderLibrary();
    const alert = await screen.findByRole('alert');
    expect(within(alert).getByRole('button', { name: /retry/i })).toBeInTheDocument();
  });

  it('messages a 401 without a pointless retry (INV-4)', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(problem(401, 'Unauthorized'));
    renderLibrary();
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(/session expired/i);
    expect(within(alert).queryByRole('button', { name: /retry/i })).not.toBeInTheDocument();
  });

  it('renders the SUCCESS grid with a card and its trust signals', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(json(list([makeAssistant()])));
    renderLibrary();
    const grid = await screen.findByRole('list', { name: /assistants/i });
    const card = within(grid).getByRole('article', { name: /benefits helper/i });
    expect(within(card).getByRole('heading', { name: /benefits helper/i })).toBeInTheDocument();
    expect(within(card).getByText(/published/i)).toBeInTheDocument();
    expect(within(card).getByText('search_text')).toBeInTheDocument();
  });

  it('filters by search query', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      json(list([makeAssistant(), makeAssistant({ id: 'a2', name: 'Sales copilot' })])),
    );
    const user = userEvent.setup();
    renderLibrary();
    await screen.findByRole('article', { name: /benefits helper/i });

    await user.type(screen.getByRole('searchbox', { name: /search assistants/i }), 'sales');
    await waitFor(() =>
      expect(screen.queryByRole('article', { name: /benefits helper/i })).not.toBeInTheDocument(),
    );
    expect(screen.getByRole('article', { name: /sales copilot/i })).toBeInTheDocument();
  });
});

describe('AssistantLibrary — Start chat (AC-2)', () => {
  it('POSTs /chat/sessions with the assistant_id and navigates to chat', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation((input, init) => {
      const url = String(input);
      if (init?.method === 'POST' && url.includes('/chat/sessions')) {
        return Promise.resolve(
          json(
            {
              id: 'sess-1',
              title: 'Benefits helper',
              model: 'anthropic/claude',
              owner_id: 'u1',
              message_count: 0,
              created_at: '2026-07-02T00:00:00Z',
              updated_at: '2026-07-02T00:00:00Z',
            },
            201,
          ),
        );
      }
      return Promise.resolve(json(list([makeAssistant()])));
    });
    const user = userEvent.setup();
    renderLibrary();

    const card = await screen.findByRole('article', { name: /benefits helper/i });
    await user.click(within(card).getByRole('button', { name: /start chat/i }));

    // The create-session call carried the assistant_id.
    await waitFor(() => {
      const call = fetchSpy.mock.calls.find(
        ([u, i]) => String(u).includes('/chat/sessions') && i?.method === 'POST',
      );
      expect(call).toBeDefined();
      expect(JSON.parse(String(call?.[1]?.body))).toEqual({ assistant_id: 'a1' });
    });

    // …and the workspace navigated to `/` with the new session bound.
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('/'));
    expect(useChatStore.getState().activeSessionId).toBe('sess-1');
  });

  it('surfaces a per-card error when create-session fails (404/403 → no crash, AC-5)', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation((input, init) => {
      const url = String(input);
      if (init?.method === 'POST' && url.includes('/chat/sessions')) {
        return Promise.resolve(problem(404, 'Not Found'));
      }
      return Promise.resolve(json(list([makeAssistant()])));
    });
    const user = userEvent.setup();
    renderLibrary();

    const card = await screen.findByRole('article', { name: /benefits helper/i });
    await user.click(within(card).getByRole('button', { name: /start chat/i }));

    expect(await within(card).findByRole('alert')).toHaveTextContent(/could not start a chat/i);
    // Did not navigate.
    expect(screen.getByTestId('location')).toHaveTextContent('/assistants');
  });
});
