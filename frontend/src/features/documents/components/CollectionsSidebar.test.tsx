/**
 * CollectionsSidebar (#49 AC-1) across EVERY state (frontend/AGENTS.md): loading,
 * error (with retry), empty, success; plus create / rename / delete and the
 * cross-tenant 404 negative (INV-1).
 */
import { describe, it, expect, vi, afterEach } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderWithQuery } from '@/test/renderWithQuery';
import { CollectionsSidebar } from './CollectionsSidebar';

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}
function problem(status: number, title: string, detail?: string): Response {
  return new Response(JSON.stringify({ type: 'about:blank', title, status, detail }), {
    status,
    headers: { 'Content-Type': 'application/problem+json' },
  });
}

const collection = (over: Partial<Record<string, unknown>> = {}) => ({
  id: 'col-1',
  name: 'Acme contracts',
  owner_id: 'u-1',
  document_count: 3,
  created_at: '2026-06-18T00:00:00Z',
  updated_at: '2026-06-18T00:00:00Z',
  ...over,
});

afterEach(() => vi.restoreAllMocks());

describe('CollectionsSidebar', () => {
  it('shows a loading state then the collections (success)', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      jsonResponse({ items: [collection()], next_cursor: null }),
    );
    renderWithQuery(<CollectionsSidebar selectedId={undefined} onSelect={() => {}} />);
    expect(screen.getByText(/loading collections/i)).toBeInTheDocument();
    expect(await screen.findByText('Acme contracts')).toBeInTheDocument();
  });

  it('shows an empty state when there are no collections', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse({ items: [], next_cursor: null }));
    renderWithQuery(<CollectionsSidebar selectedId={undefined} onSelect={() => {}} />);
    expect(await screen.findByText(/no collections yet/i)).toBeInTheDocument();
  });

  it('shows an actionable error with retry on failure', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(problem(500, 'Server Error', 'boom'));
    renderWithQuery(<CollectionsSidebar selectedId={undefined} onSelect={() => {}} />);
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(/couldn’t load collections/i);
    expect(within(alert).getByRole('button', { name: /retry/i })).toBeInTheDocument();
  });

  it('selects a collection when clicked (AC-1)', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      jsonResponse({ items: [collection()], next_cursor: null }),
    );
    const onSelect = vi.fn();
    const user = userEvent.setup();
    renderWithQuery(<CollectionsSidebar selectedId={undefined} onSelect={onSelect} />);
    await user.click(await screen.findByText('Acme contracts'));
    expect(onSelect).toHaveBeenCalledWith('col-1');
  });

  it('creates a collection via POST /collections (AC-1)', async () => {
    const fetchSpy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(jsonResponse({ items: [], next_cursor: null }))
      .mockResolvedValueOnce(jsonResponse(collection({ id: 'col-new', name: 'New' }), 201))
      .mockResolvedValue(jsonResponse({ items: [collection({ id: 'col-new', name: 'New' })] }));
    const user = userEvent.setup();
    renderWithQuery(<CollectionsSidebar selectedId={undefined} onSelect={() => {}} />);

    await screen.findByText(/no collections yet/i);
    await user.type(screen.getByPlaceholderText(/new collection/i), 'New');
    await user.click(screen.getByRole('button', { name: /^add$/i }));

    await waitFor(() => {
      const post = fetchSpy.mock.calls.find((c) => (c[1] as RequestInit)?.method === 'POST');
      expect(post).toBeTruthy();
      expect(JSON.parse((post?.[1] as RequestInit).body as string)).toEqual({ name: 'New' });
    });
  });

  it('renames a collection via PATCH (AC-1)', async () => {
    const fetchSpy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(jsonResponse({ items: [collection()], next_cursor: null }))
      .mockResolvedValue(jsonResponse(collection({ name: 'Renamed' })));
    const user = userEvent.setup();
    renderWithQuery(<CollectionsSidebar selectedId="col-1" onSelect={() => {}} />);

    await screen.findByText('Acme contracts');
    await user.click(screen.getByRole('button', { name: /rename acme contracts/i }));
    const input = await screen.findByLabelText(/rename acme contracts/i);
    await user.clear(input);
    await user.type(input, 'Renamed');
    await user.click(screen.getByRole('button', { name: /save/i }));

    await waitFor(() => {
      const patch = fetchSpy.mock.calls.find((c) => (c[1] as RequestInit)?.method === 'PATCH');
      expect(patch).toBeTruthy();
    });
  });

  it('deletes a collection after confirming in the dialog; a 404 surfaces inline (INV-1)', async () => {
    const fetchSpy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(jsonResponse({ items: [collection()], next_cursor: null }))
      .mockResolvedValueOnce(problem(404, 'Not Found'))
      .mockResolvedValue(jsonResponse({ items: [collection()], next_cursor: null }));
    const user = userEvent.setup();
    renderWithQuery(<CollectionsSidebar selectedId="col-1" onSelect={() => {}} />);

    await screen.findByText('Acme contracts');
    // Clicking the trash opens the focus-managed confirm dialog (warns the cascade).
    await user.click(screen.getByRole('button', { name: /delete acme contracts/i }));
    const dialog = await screen.findByRole('alertdialog');
    expect(within(dialog).getByText(/and all its documents/i)).toBeInTheDocument();

    // Confirming fires DELETE; the 404 surfaces inline.
    await user.click(within(dialog).getByRole('button', { name: /^delete$/i }));
    await waitFor(() => {
      const del = fetchSpy.mock.calls.find((c) => (c[1] as RequestInit)?.method === 'DELETE');
      expect(del?.[0]).toContain('/collections/col-1');
    });
    expect(await screen.findByText(/delete failed|not found/i)).toBeInTheDocument();
  });

  it('does not delete a collection when the dialog is cancelled', async () => {
    const fetchSpy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(jsonResponse({ items: [collection()], next_cursor: null }));
    const user = userEvent.setup();
    renderWithQuery(<CollectionsSidebar selectedId="col-1" onSelect={() => {}} />);

    await screen.findByText('Acme contracts');
    await user.click(screen.getByRole('button', { name: /delete acme contracts/i }));
    const dialog = await screen.findByRole('alertdialog');
    await user.click(within(dialog).getByRole('button', { name: /cancel/i }));

    await waitFor(() => expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument());
    expect(fetchSpy.mock.calls.some((c) => (c[1] as RequestInit)?.method === 'DELETE')).toBe(false);
  });
});
