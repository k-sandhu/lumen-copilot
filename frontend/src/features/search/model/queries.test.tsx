/**
 * useSearch (#84) — TanStack Query over the `search` api/ boundary, against a
 * mocked fetch. Asserts: the query is disabled until `q` is non-empty (so the
 * empty/422 path never fires on mount); a submitted query hits GET /search with
 * the trimmed `q`; and the spec-0004 negative categories surface as errors the
 * UI can branch on (401 INV-4, 422 INV-8).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import { setAccessToken, clearAccessToken } from '@/api';
import type { CollectionList, SearchResponse } from '@/api';
import { searchKey, useSearch, useSearchCollections } from './queries';

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

function wrapper() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0, staleTime: 0 } },
  });
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
}

const empty: SearchResponse = { query: 'q', results: [], hidden_count: 0 };

beforeEach(() => setAccessToken('jwt'));
afterEach(() => {
  clearAccessToken();
  vi.restoreAllMocks();
});

describe('searchKey', () => {
  it('trims q and includes filters so distinct queries cache separately', () => {
    expect(searchKey({ q: '  pto  ', source: 'upload' })).toEqual([
      'search',
      'pto',
      null,
      'upload',
      null,
      null,
      null,
    ]);
  });
});

describe('useSearch', () => {
  it('does NOT fetch while q is empty (no 422-on-empty fired from mount)', async () => {
    const spy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(json(empty));
    const { result } = renderHook(() => useSearch({ q: '   ' }), { wrapper: wrapper() });
    // Give any (incorrect) request a tick to fire.
    await new Promise((r) => setTimeout(r, 0));
    expect(spy).not.toHaveBeenCalled();
    expect(result.current.fetchStatus).toBe('idle');
  });

  it('fetches GET /search with the trimmed q once submitted', async () => {
    const spy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(json({ ...empty, query: 'roadmap' }));
    const { result } = renderHook(() => useSearch({ q: '  roadmap  ' }), { wrapper: wrapper() });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    const url = String(spy.mock.calls.at(-1)?.[0]);
    expect(url).toContain('/search');
    expect(url).toContain('q=roadmap');
  });

  it('surfaces a 401 as an error (INV-4)', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(problem(401, 'Unauthorized'));
    const { result } = renderHook(() => useSearch({ q: 'x' }), { wrapper: wrapper() });
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error).toMatchObject({ status: 401 });
  });

  it('surfaces a 422 as an error (INV-8)', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(problem(422, 'Unprocessable Entity'));
    const { result } = renderHook(() => useSearch({ q: 'x' }), { wrapper: wrapper() });
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error).toMatchObject({ status: 422 });
  });
});

describe('useSearchCollections', () => {
  it('loads the caller’s collections from GET /collections for the scope filter', async () => {
    const list: CollectionList = { items: [], next_cursor: null };
    const spy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(json(list));
    const { result } = renderHook(() => useSearchCollections(), { wrapper: wrapper() });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(String(spy.mock.calls.at(-1)?.[0])).toContain('/collections');
    expect(result.current.data?.items).toEqual([]);
  });
});
