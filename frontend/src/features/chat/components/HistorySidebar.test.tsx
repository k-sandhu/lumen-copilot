/**
 * HistorySidebar (AC-4) across every state (frontend/AGENTS.md): loading, empty,
 * error+retry, populated; new/rename/delete. Drives the real query hooks against
 * a mocked fetch so a contract match is an integration match (ADR-0006 Phase 1).
 *
 * Negative: a 404 listing (cross-tenant / not-permitted — spec 0004 INV-1/INV-2)
 * surfaces as an actionable error, never a blank pane.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderWithQuery } from '@/test/renderWithQuery';
import { setAccessToken, clearAccessToken } from '@/api';
import { HistorySidebar } from './HistorySidebar';

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

const SESSIONS = {
  items: [
    {
      id: 's1',
      title: 'Budget questions',
      model: 'm1',
      owner_id: 'u1',
      message_count: 4,
      created_at: '2026-06-18T00:00:00Z',
      updated_at: '2026-06-18T00:00:00Z',
    },
  ],
  next_cursor: null,
};

beforeEach(() => setAccessToken('jwt'));
afterEach(() => {
  clearAccessToken();
  vi.restoreAllMocks();
});

function render(props: Partial<React.ComponentProps<typeof HistorySidebar>> = {}) {
  return renderWithQuery(
    <HistorySidebar
      activeSessionId={null}
      onSelect={props.onSelect ?? (() => {})}
      onCreated={props.onCreated ?? (() => {})}
      onDeleted={props.onDeleted ?? (() => {})}
    />,
  );
}

describe('HistorySidebar', () => {
  it('shows a loading state then the session list (AC-4)', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(json(SESSIONS));
    render();
    expect(screen.getByRole('status')).toHaveTextContent(/loading chats/i);
    expect(await screen.findByText('Budget questions')).toBeInTheDocument();
  });

  it('shows an empty state when there are no sessions', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(json({ items: [], next_cursor: null }));
    render();
    expect(await screen.findByText(/no chats yet/i)).toBeInTheDocument();
  });

  it('surfaces a load error with a Retry (negative: 404 INV-1/INV-2)', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(problem(404, 'Not found'));
    render();
    const alert = await screen.findByRole('alert');
    expect(within(alert).getByRole('button', { name: /retry/i })).toBeInTheDocument();
  });

  it('creates a new chat and reports its id (AC-4 new)', async () => {
    const onCreated = vi.fn();
    const fetchSpy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(json(SESSIONS))
      .mockResolvedValueOnce(
        json(
          {
            id: 's-new',
            title: 'New chat',
            model: 'm1',
            owner_id: 'u1',
            message_count: 0,
            created_at: '2026-06-18T00:00:00Z',
            updated_at: '2026-06-18T00:00:00Z',
          },
          201,
        ),
      )
      .mockResolvedValue(json(SESSIONS));
    render({ onCreated });
    const user = userEvent.setup();
    await screen.findByText('Budget questions');
    await user.click(screen.getByRole('button', { name: /\+ new/i }));
    await waitFor(() => expect(onCreated).toHaveBeenCalledWith('s-new'));
    const createCall = fetchSpy.mock.calls.find(
      (c) => (c[1] as RequestInit)?.method === 'POST',
    );
    expect(createCall).toBeTruthy();
  });

  it('renames a session via PATCH (AC-4 rename)', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(json(SESSIONS));
    render();
    const user = userEvent.setup();
    await screen.findByText('Budget questions');
    await user.click(screen.getByRole('button', { name: /rename budget questions/i }));
    const input = screen.getByRole('textbox', { name: /rename chat/i });
    await user.clear(input);
    await user.type(input, 'Renamed{Enter}');
    await waitFor(() => {
      const patch = fetchSpy.mock.calls.find((c) => (c[1] as RequestInit)?.method === 'PATCH');
      expect(patch).toBeTruthy();
      expect(JSON.parse((patch?.[1] as RequestInit).body as string)).toMatchObject({
        title: 'Renamed',
      });
    });
  });

  it('confirms and deletes a session (AC-4 delete)', async () => {
    const onDeleted = vi.fn();
    const fetchSpy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(json(SESSIONS))
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
      .mockResolvedValue(json({ items: [], next_cursor: null }));
    render({ onDeleted });
    const user = userEvent.setup();
    await screen.findByText('Budget questions');
    await user.click(screen.getByRole('button', { name: /delete budget questions/i }));
    // A confirm dialog appears before anything is deleted.
    const dialog = await screen.findByRole('alertdialog', { name: /confirm delete/i });
    await user.click(within(dialog).getByRole('button', { name: /^delete$/i }));
    await waitFor(() => expect(onDeleted).toHaveBeenCalled());
    const del = fetchSpy.mock.calls.find((c) => (c[1] as RequestInit)?.method === 'DELETE');
    expect(del).toBeTruthy();
  });
});
