/**
 * SearchTypeahead (spec 0005, #144): the search box as an accessible combobox.
 * Empty → Recent + Saved groups; typing → a "Search for …" lead + permission-
 * trimmed server suggestions. Choosing runs the query (saved → applies filters);
 * keyboard nav works. Drives the real api/ client against a mocked fetch.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderWithQuery } from '@/test/renderWithQuery';
import { setAccessToken, clearAccessToken } from '@/api';
import { SearchTypeahead } from './SearchTypeahead';

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

const SAVED = {
  id: 's1',
  name: 'Acme deals',
  query: 'acme',
  source: 'upload',
  owner_id: 'u1',
  created_at: '2026-06-20T00:00:00Z',
  updated_at: '2026-06-20T00:00:00Z',
};

function mockApi() {
  return vi.spyOn(globalThis, 'fetch').mockImplementation((input) => {
    const url = typeof input === 'string' ? input : (input as Request).url;
    if (url.includes('/search/recent'))
      return Promise.resolve(json({ items: [{ query: 'acme renewal', last_used_at: 'x' }] }));
    if (url.includes('/saved-searches')) return Promise.resolve(json({ items: [SAVED] }));
    if (url.includes('/search/suggest'))
      return Promise.resolve(
        json({
          suggestions: [
            { kind: 'completion', text: 'acme renewal' },
            { kind: 'document', text: 'Acme Memo.pdf', document_id: 'd1' },
          ],
        }),
      );
    return Promise.resolve(json({}));
  });
}

function setup(value: string, overrides: Partial<React.ComponentProps<typeof SearchTypeahead>> = {}) {
  const onRunQuery = vi.fn();
  const onApplySaved = vi.fn();
  renderWithQuery(
    <SearchTypeahead
      value={value}
      onChange={vi.fn()}
      onRunQuery={onRunQuery}
      onApplySaved={onApplySaved}
      {...overrides}
    />,
  );
  return { onRunQuery, onApplySaved };
}

beforeEach(() => setAccessToken('jwt'));
afterEach(() => {
  clearAccessToken();
  vi.restoreAllMocks();
});

describe('SearchTypeahead', () => {
  it('empty box offers Recent + Saved; choosing a recent runs it', async () => {
    mockApi();
    const { onRunQuery } = setup('');
    const user = userEvent.setup();
    await user.click(screen.getByRole('combobox'));
    const recent = await screen.findByRole('option', { name: /acme renewal/i });
    await user.click(recent);
    expect(onRunQuery).toHaveBeenCalledWith('acme renewal');
  });

  it('choosing a saved search applies it (query + filters)', async () => {
    mockApi();
    const { onApplySaved } = setup('');
    const user = userEvent.setup();
    await user.click(screen.getByRole('combobox'));
    const saved = await screen.findByRole('option', { name: /acme deals/i });
    await user.click(saved);
    expect(onApplySaved).toHaveBeenCalledWith(expect.objectContaining({ name: 'Acme deals', query: 'acme' }));
  });

  it('typing shows server suggestions; choosing a document runs its title', async () => {
    mockApi();
    const { onRunQuery } = setup('acme');
    const user = userEvent.setup();
    await user.click(screen.getByRole('combobox'));
    const doc = await screen.findByRole('option', { name: /Acme Memo\.pdf/i });
    await user.click(doc);
    expect(onRunQuery).toHaveBeenCalledWith('Acme Memo.pdf');
  });

  it('Enter with no active option runs the typed query (degrades gracefully)', async () => {
    mockApi();
    const { onRunQuery } = setup('acme');
    const user = userEvent.setup();
    const box = screen.getByRole('combobox');
    await user.click(box);
    await waitFor(() => expect(box).toHaveAttribute('aria-expanded', 'true'));
    await user.keyboard('{Enter}');
    expect(onRunQuery).toHaveBeenCalledWith('acme');
  });

  it('clears recent history from the Recent group', async () => {
    const spy = mockApi();
    setup('');
    const user = userEvent.setup();
    await user.click(screen.getByRole('combobox'));
    await screen.findByRole('option', { name: /acme renewal/i });
    await user.click(screen.getByRole('button', { name: /clear/i }));
    await waitFor(() =>
      expect(spy.mock.calls.some((c) => (c[1] as RequestInit)?.method === 'DELETE')).toBe(true),
    );
  });
});
