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
import type { CollectionList, SearchResponse } from '@/api';
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

/** The filter sidebar loads the caller's collections on mount (an empty list here). */
const emptyCollections: CollectionList = { items: [], next_cursor: null };

function urlOf(input: RequestInfo | URL): string {
  return typeof input === 'string' ? input : input instanceof URL ? input.href : input.url;
}

/**
 * Route `/collections` (the filter sidebar) vs `/search` so the search response
 * under test is never swallowed by the collections fetch. `searchResponse` is the
 * Response (or a factory) the `/search` call resolves to; the factory receives the
 * request URL so a test can assert / honor the server-side filter params (the
 * content-type facet is a server `type` param — spec 0004 INV-3, #118).
 */
function mockSearch(
  searchResponse: Response | ((url: string) => Response | Promise<Response>),
) {
  return vi.spyOn(globalThis, 'fetch').mockImplementation((input) => {
    const url = urlOf(input);
    // The typeahead fires suggest/recent/saved on mount + as you type; route them
    // to FRESH empty responses so they never consume the single `/search` mock
    // Response (a Response body can be read once).
    if (url.includes('/search/suggest')) return Promise.resolve(json({ suggestions: [] }));
    if (url.includes('/search/recent')) return Promise.resolve(json({ items: [] }));
    if (url.includes('/saved-searches')) return Promise.resolve(json({ items: [] }));
    if (url.includes('/collections')) return Promise.resolve(json(emptyCollections));
    return Promise.resolve(
      typeof searchResponse === 'function' ? searchResponse(url) : searchResponse,
    );
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

/** A response spanning two REAL source kinds + content types, for the facet test. */
const multiSourceResponse: SearchResponse = {
  query: 'pricing',
  results: [
    {
      id: 'r1',
      title: 'PTO Policy 2026',
      snippet: 'Employees accrue 20 days of paid time off per year.',
      match_spans: [],
      why_matched: 'title',
      source: 'upload',
      type: 'document',
      owner: 'Dana Ruiz',
      last_indexed: '2026-06-18T00:00:00Z',
      permission: 'allowed',
    },
    {
      id: 'r2',
      title: 'Pricing thread',
      snippet: 'The Q3 change is locked.',
      match_spans: [],
      why_matched: 'body',
      source: 'chat',
      type: 'message',
      owner: 'Marcus Lee',
      last_indexed: '2026-06-10T00:00:00Z',
      permission: 'allowed',
    },
  ],
  hidden_count: 0,
};

async function runSearch(term = 'pto policy') {
  const user = userEvent.setup();
  // The search box is now an accessible combobox; Enter runs the typed query
  // (no separate Search button — choosing a suggestion or pressing Enter submits).
  await user.type(screen.getByRole('combobox'), `${term}{Enter}`);
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
    // The `/search` call is deferred; `/collections` resolves immediately so the
    // sidebar never blocks the loading assertion.
    let resolve!: (r: Response) => void;
    const deferred = new Promise<Response>((r) => {
      resolve = r;
    });
    vi.spyOn(globalThis, 'fetch').mockImplementation((input) => {
      const url = urlOf(input);
      if (url.includes('/search/suggest')) return Promise.resolve(json({ suggestions: [] }));
      if (url.includes('/search/recent')) return Promise.resolve(json({ items: [] }));
      if (url.includes('/saved-searches')) return Promise.resolve(json({ items: [] }));
      if (url.includes('/collections')) return Promise.resolve(json(emptyCollections));
      return deferred;
    });
    renderWithQuery(<SearchScreen />);
    await runSearch();

    expect(await screen.findByLabelText('Searching')).toBeInTheDocument();

    resolve(json(fullResponse));
    expect(await screen.findByRole('article', { name: /PTO Policy 2026/i })).toBeInTheDocument();
  });

  it('renders the cited direct answer + ranked rows + the trim notice on SUCCESS', async () => {
    mockSearch(json(fullResponse));
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
    mockSearch(json({ query: 'no hits here', results: [], hidden_count: 0 } satisfies SearchResponse));
    renderWithQuery(<SearchScreen />);
    await runSearch('no hits here');
    expect(await screen.findByText(/no results for/i)).toBeInTheDocument();
    expect(screen.getByText(/“no hits here”/)).toBeInTheDocument();
  });

  it('shows an actionable ERROR with retry on a 401 (INV-4)', async () => {
    mockSearch(problem(401, 'Unauthorized'));
    renderWithQuery(<SearchScreen />);
    await runSearch();
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(/session expired/i);
    expect(within(alert).getByRole('button', { name: /try again/i })).toBeInTheDocument();
  });

  it('shows a rephrase ERROR on a 422 (INV-8)', async () => {
    mockSearch(problem(422, 'Unprocessable Entity'));
    renderWithQuery(<SearchScreen />);
    await runSearch();
    expect(await screen.findByRole('alert')).toHaveTextContent(/couldn’t be understood/i);
  });

  it('retries the search when Try again is clicked', async () => {
    // `/search` fails once (500) then succeeds; `/collections` always resolves.
    let searchCalls = 0;
    const spy = vi.spyOn(globalThis, 'fetch').mockImplementation((input) => {
      const url = urlOf(input);
      if (url.includes('/search/suggest')) return Promise.resolve(json({ suggestions: [] }));
      if (url.includes('/search/recent')) return Promise.resolve(json({ items: [] }));
      if (url.includes('/saved-searches')) return Promise.resolve(json({ items: [] }));
      if (url.includes('/collections')) return Promise.resolve(json(emptyCollections));
      searchCalls += 1;
      return Promise.resolve(searchCalls === 1 ? problem(500, 'Server Error') : json(fullResponse));
    });
    renderWithQuery(<SearchScreen />);
    const user = await runSearch();

    const alert = await screen.findByRole('alert');
    await user.click(within(alert).getByRole('button', { name: /try again/i }));

    expect(await screen.findByRole('article', { name: /PTO Policy 2026/i })).toBeInTheDocument();
    expect(spy.mock.calls.length).toBeGreaterThanOrEqual(2);
  });

  it('filters the source facet and the result count from the data, never an invented connector', async () => {
    // The content-type facet is a SERVER `type` param (#118): honor it in the mock
    // so the narrowed query returns only the matching rows (the server re-derives
    // results/direct_answer/hidden_count over the narrowed set).
    const spy = mockSearch((url) => {
      const type = new URL(url, 'http://localhost').searchParams.get('type');
      if (!type) return json(multiSourceResponse);
      return json({
        ...multiSourceResponse,
        results: multiSourceResponse.results.filter((r) => r.type === type),
      } satisfies SearchResponse);
    });
    renderWithQuery(<SearchScreen />);
    await runSearch();

    // The filter sidebar shows only the source kinds the contract serves —
    // derived from the data — never Slack/Jira/Tickets/Code/People.
    const filters = await screen.findByRole('navigation', { name: /search filters/i });
    expect(within(filters).getByText('Uploaded documents')).toBeInTheDocument();
    expect(within(filters).getByText('Chat messages')).toBeInTheDocument();
    expect(within(filters).queryByText(/slack|jira|tickets|github|salesforce|people/i)).toBeNull();

    // Narrow to the "message" content type → the server query carries `type=message`
    // and only the chat result remains.
    await userEvent.setup().click(within(filters).getByRole('checkbox', { name: /^message/i }));
    expect(
      await screen.findByRole('heading', { name: /pricing thread/i }),
    ).toBeInTheDocument();
    const list = screen.getByRole('list', { name: /search results/i });
    expect(within(list).queryByRole('heading', { name: /PTO Policy/i })).toBeNull();

    // The narrowing rode the SERVER param, not a client-only filter.
    const typedCall = spy.mock.calls
      .map(([input]) => urlOf(input))
      .some((u) => u.includes('/search') && u.includes('type=message'));
    expect(typedCall).toBe(true);
  });

  it('keeps the answer coherent under a content-type filter — never a visible answer with dropped citations (INV-3, #118)', async () => {
    // A cited answer whose ONLY citation points at the document row. Selecting the
    // "message" content type must NOT leave that answer visible with its citation
    // un-resolvable: the server re-derives the response over the narrowed set, so
    // the chat-only page carries no document-citing answer.
    const citedDocResponse: SearchResponse = {
      query: 'pricing',
      results: multiSourceResponse.results,
      direct_answer: {
        text: 'The PTO policy grants 20 days [1].',
        citations: [{ result_id: 'r1', snippet: 'accrue 20 days' }],
      },
      hidden_count: 0,
    };
    // The server returns ONLY the chat row (and no doc-citing answer) once `type`
    // narrows to "message" — modelling a coherent server re-derivation.
    const narrowedResponse: SearchResponse = {
      query: 'pricing',
      results: multiSourceResponse.results.filter((r) => r.type === 'message'),
      hidden_count: 0,
    };
    mockSearch((url) => {
      const type = new URL(url, 'http://localhost').searchParams.get('type');
      return json(type === 'message' ? narrowedResponse : citedDocResponse);
    });
    renderWithQuery(<SearchScreen />);
    await runSearch('pricing');

    // Baseline: the cited answer is present and its [1] citation resolves.
    const answer = await screen.findByRole('region', { name: /direct answer/i });
    expect(within(answer).getByText(/PTO policy grants/i)).toBeInTheDocument();
    expect(within(answer).getByRole('button', { name: /citation 1/i })).toBeInTheDocument();

    // Narrow to "message": the document row (and its citation) is gone — so the
    // answer must NOT linger with a dropped/literal citation. The server returned
    // no answer for the narrowed set, so the direct-answer region is gone too.
    const filters = await screen.findByRole('navigation', { name: /search filters/i });
    await userEvent.setup().click(within(filters).getByRole('checkbox', { name: /^message/i }));

    expect(await screen.findByRole('heading', { name: /pricing thread/i })).toBeInTheDocument();
    expect(screen.queryByRole('region', { name: /direct answer/i })).toBeNull();
    // No literal "[1]" left dangling in the body either.
    expect(screen.queryByText(/PTO policy grants/i)).toBeNull();
  });

  it('keeps the filter sidebar visible (and clearable) on a filter-empty result', async () => {
    // A scoped query that narrows to zero rows must still show the sidebar so the
    // active scope is visible and resettable — not a bare empty state.
    mockSearch((url) => {
      const source = new URL(url, 'http://localhost').searchParams.get('source');
      // The "upload" source returns nothing for this query; unfiltered returns both
      // source kinds so the "Uploaded documents" facet is available to click.
      if (source === 'upload') return json({ query: 'pricing', results: [], hidden_count: 0 });
      if (source) {
        return json({
          ...multiSourceResponse,
          results: multiSourceResponse.results.filter((r) => r.source === source),
        } satisfies SearchResponse);
      }
      return json(multiSourceResponse);
    });
    renderWithQuery(<SearchScreen />);
    await runSearch('pricing');

    const filters = await screen.findByRole('navigation', { name: /search filters/i });
    await userEvent.setup().click(
      within(filters).getByRole('checkbox', { name: /uploaded documents/i }),
    );

    // Filtered-empty: a status (not the bare "no results for" empty) AND the sidebar
    // is still on screen so the scope can be cleared.
    expect(await screen.findByText(/under the active filters/i)).toBeInTheDocument();
    expect(screen.getByRole('navigation', { name: /search filters/i })).toBeInTheDocument();
    const clear = screen.getByRole('button', { name: /clear filters/i });
    await userEvent.setup().click(clear);

    // Clearing the scope brings the chat result back.
    expect(await screen.findByRole('heading', { name: /pricing thread/i })).toBeInTheDocument();
  });
});
