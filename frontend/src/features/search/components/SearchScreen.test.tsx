/**
 * SearchScreen (#84) — the feature root across EVERY state (frontend/AGENTS.md):
 * initial (no query) / loading / error (actionable retry) / empty / success with
 * direct answer + ranked rows + the permission-trim notice. Renders against a
 * mocked fetch so a contract match is an integration match (ADR-0006 Phase 1).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderWithQuery } from '@/test/renderWithQuery';
import { setAccessToken, clearAccessToken } from '@/api';
import type { SearchResponse } from '@/api';
import { SearchScreen } from './SearchScreen';

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

const fullResponse: SearchResponse = {
  query: 'pto policy',
  results: [
    {
      id: 'r1',
      title: 'PTO Policy 2026',
      snippet: 'Employees accrue 20 days of paid time off per year.',
      match_spans: [{ start: 0, end: 9 }],
      why_matched: 'semantic + title',
      source: 'upload',
      type: 'document',
      owner: 'Dana Ruiz',
      last_indexed: '2026-06-18T00:00:00Z',
      permission: 'allowed',
    },
  ],
  direct_answer: {
    text: 'You accrue **20 days** of PTO.',
    citations: [{ result_id: 'r1', snippet: 'accrue 20 days' }],
  },
  hidden_count: 3,
};

async function runSearch(term = 'pto policy') {
  const user = userEvent.setup();
  await user.type(screen.getByRole('searchbox'), term);
  await user.click(screen.getByRole('button', { name: /^search$/i }));
  return user;
}

beforeEach(() => setAccessToken('jwt'));
afterEach(() => {
  clearAccessToken();
  vi.restoreAllMocks();
});

describe('SearchScreen', () => {
  it('shows the INITIAL prompt before any query is submitted', () => {
    renderWithQuery(<SearchScreen />);
    expect(screen.getByText(/search across your connected sources/i)).toBeInTheDocument();
    expect(screen.getByText(/permission-trimmed/i)).toBeInTheDocument();
  });

  it('renders a LOADING skeleton, then the SUCCESS results', async () => {
    let resolve!: (r: Response) => void;
    vi.spyOn(globalThis, 'fetch').mockReturnValue(
      new Promise<Response>((r) => {
        resolve = r;
      }),
    );
    renderWithQuery(<SearchScreen />);
    await runSearch();

    expect(await screen.findByLabelText('Searching')).toBeInTheDocument();

    resolve(json(fullResponse));
    expect(await screen.findByRole('article', { name: /PTO Policy 2026/i })).toBeInTheDocument();
  });

  it('renders the cited direct answer + ranked rows + the trim notice on SUCCESS', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(json(fullResponse));
    renderWithQuery(<SearchScreen />);
    await runSearch();

    // Direct answer (rendered markdown).
    const answer = await screen.findByRole('region', { name: /direct answer/i });
    expect(within(answer).getByText('20 days').tagName).toBe('STRONG');

    // Ranked row with its trust signals.
    const list = screen.getByRole('list', { name: /search results/i });
    expect(within(list).getByRole('heading', { name: 'PTO Policy 2026' })).toBeInTheDocument();
    expect(within(list).getByText(/you have access/i)).toBeInTheDocument();

    // Permission trim notice from hidden_count.
    expect(screen.getByRole('note', { name: /permission trim/i })).toHaveTextContent(
      "3 results hidden — you don't have access",
    );
  });

  it('shows the EMPTY state for a submitted query with no results', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      json({ query: 'no hits here', results: [], hidden_count: 0 } satisfies SearchResponse),
    );
    renderWithQuery(<SearchScreen />);
    await runSearch('no hits here');
    expect(await screen.findByText(/no results for/i)).toBeInTheDocument();
    expect(screen.getByText(/“no hits here”/)).toBeInTheDocument();
  });

  it('shows an actionable ERROR with retry on a 401 (INV-4)', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(problem(401, 'Unauthorized'));
    renderWithQuery(<SearchScreen />);
    await runSearch();
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(/session expired/i);
    expect(within(alert).getByRole('button', { name: /try again/i })).toBeInTheDocument();
  });

  it('shows a rephrase ERROR on a 422 (INV-8)', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(problem(422, 'Unprocessable Entity'));
    renderWithQuery(<SearchScreen />);
    await runSearch();
    expect(await screen.findByRole('alert')).toHaveTextContent(/couldn’t be understood/i);
  });

  it('retries the search when Try again is clicked', async () => {
    const spy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(problem(500, 'Server Error'))
      .mockResolvedValue(json(fullResponse));
    renderWithQuery(<SearchScreen />);
    const user = await runSearch();

    const alert = await screen.findByRole('alert');
    await user.click(within(alert).getByRole('button', { name: /try again/i }));

    expect(await screen.findByRole('article', { name: /PTO Policy 2026/i })).toBeInTheDocument();
    expect(spy.mock.calls.length).toBeGreaterThanOrEqual(2);
  });
});
