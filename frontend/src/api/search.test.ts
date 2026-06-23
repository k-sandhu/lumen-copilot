/**
 * Search api/ boundary calls against a mocked fetch. Verifies the request shapes
 * conform to the frozen contract (contracts/openapi.yaml §search, #80) and that
 * the spec-0004 negative categories surface as typed ApiErrors the UI branches on:
 *   - missing/expired token → 401 (INV-4)
 *   - malformed query (empty q) → 422 (INV-8)
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { ApiError, search, setAccessToken, clearAccessToken } from '@/api';
import type { SearchResponse } from '@/api';

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

beforeEach(() => setAccessToken('jwt'));
afterEach(() => {
  clearAccessToken();
  vi.restoreAllMocks();
});

interface FetchSpy {
  mock: { calls: unknown[][] };
}
function lastCall(spy: FetchSpy) {
  const calls = spy.mock.calls;
  const call = calls[calls.length - 1];
  return { url: String(call?.[0]), init: call?.[1] as RequestInit };
}

const emptyResponse: SearchResponse = { query: 'q', results: [], hidden_count: 0 };

describe('search api boundary', () => {
  it('GET /search sends the required q and is bearer-authenticated', async () => {
    const spy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(json(emptyResponse));
    await search({ q: 'roadmap' });
    const { url, init } = lastCall(spy);
    expect(url).toContain('/search');
    expect(url).toContain('q=roadmap');
    expect(new Headers(init.headers).get('Authorization')).toBe('Bearer jwt');
  });

  it('serializes collection_id / source / type filters + pagination', async () => {
    const spy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(json(emptyResponse));
    await search({
      q: 'q1',
      collection_id: 'c1',
      source: 'upload',
      type: 'document',
      cursor: 'abc',
      limit: 25,
    });
    const { url } = lastCall(spy);
    expect(url).toContain('collection_id=c1');
    expect(url).toContain('source=upload');
    expect(url).toContain('type=document');
    expect(url).toContain('cursor=abc');
    expect(url).toContain('limit=25');
  });

  it('parses the permission-trimmed response: hidden_count + cited direct answer', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      json({
        query: 'pto policy',
        results: [
          {
            id: 'r1',
            title: 'PTO Policy',
            snippet: 'Employees accrue 20 days.',
            match_spans: [{ start: 0, end: 9 }],
            why_matched: 'semantic + title',
            source: 'upload',
            type: 'document',
            owner: null,
            last_indexed: '2026-06-01T00:00:00Z',
            permission: 'allowed',
          },
        ],
        direct_answer: {
          text: 'You accrue 20 days.',
          citations: [{ result_id: 'r1', snippet: 'accrue 20 days' }],
        },
        hidden_count: 3,
      }),
    );
    const res = await search({ q: 'pto policy' });
    expect(res.hidden_count).toBe(3);
    expect(res.results[0]?.permission).toBe('allowed');
    expect(res.direct_answer?.citations[0]?.result_id).toBe('r1');
  });

  it('missing/expired token → 401 ApiError (INV-4)', async () => {
    clearAccessToken();
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(problem(401, 'Unauthorized'));
    await expect(search({ q: 'x' })).rejects.toMatchObject({ status: 401 });
    await expect(search({ q: 'x' })).rejects.toBeInstanceOf(ApiError);
  });

  it('malformed query (empty q) → 422 ApiError (INV-8)', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(problem(422, 'Unprocessable Entity'));
    await expect(search({ q: '' })).rejects.toMatchObject({ status: 422 });
  });
});
