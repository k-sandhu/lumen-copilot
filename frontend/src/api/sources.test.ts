/**
 * Sources api/ boundary calls against a mocked fetch. Verifies the request shapes
 * conform to the frozen contract (contracts/openapi.yaml §sources, ADR-0009 / #108)
 * and that the spec-0004 negative categories surface as typed ApiErrors the UI
 * branches on:
 *   - missing/expired token → 401 (INV-4)
 *   - invalid / SSRF-blocked URL on add → 422 (INV-8, ADR-0009 §3)
 *   - non-owner / cross-tenant source on sync/delete → 404 (INV-1/INV-2)
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import {
  ApiError,
  listSources,
  createSource,
  syncSource,
  deleteSource,
  setAccessToken,
  clearAccessToken,
} from '@/api';
import type { Source } from '@/api';

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}
function problem(status: number, title: string, code?: string): Response {
  return new Response(JSON.stringify({ type: 'about:blank', title, status, code }), {
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

const sampleSource: Source = {
  id: '11111111-1111-1111-1111-111111111111',
  type: 'web',
  config: { url: 'https://example.com/handbook', mode: 'page' },
  status: 'pending',
  indexed_count: 0,
  last_synced_at: null,
  owner_id: '22222222-2222-2222-2222-222222222222',
  created_at: '2026-06-23T00:00:00Z',
  updated_at: '2026-06-23T00:00:00Z',
};

describe('sources api boundary', () => {
  it('GET /sources paginates + is bearer-authenticated', async () => {
    const spy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(json({ items: [] }));
    await listSources({ cursor: 'c', limit: 10 });
    const { url, init } = lastCall(spy);
    expect(url).toContain('/sources');
    expect(url).toContain('cursor=c');
    expect(url).toContain('limit=10');
    expect(new Headers(init.headers).get('Authorization')).toBe('Bearer jwt');
  });

  it('parses a connector-grid page (type, config, status, indexed_count)', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      json({
        items: [
          {
            ...sampleSource,
            status: 'ready',
            indexed_count: 12,
            last_synced_at: '2026-06-23T01:00:00Z',
          },
        ],
        next_cursor: null,
      }),
    );
    const res = await listSources();
    expect(res.items[0]?.type).toBe('web');
    expect(res.items[0]?.config.mode).toBe('page');
    expect(res.items[0]?.status).toBe('ready');
    expect(res.items[0]?.indexed_count).toBe(12);
  });

  it('POST /sources adds a web source ({type, url}) → pending', async () => {
    const spy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(json(sampleSource, 201));
    const created = await createSource({ type: 'web', url: 'https://example.com/handbook' });
    expect(created.status).toBe('pending');
    const { url, init } = lastCall(spy);
    expect(url).toContain('/sources');
    expect(init.method).toBe('POST');
    expect(JSON.parse(init.body as string)).toEqual({
      type: 'web',
      url: 'https://example.com/handbook',
    });
  });

  it('an invalid / SSRF-blocked URL on add → 422 ApiError (INV-8, ADR-0009 §3)', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      problem(422, 'Unprocessable Entity', 'url_blocked'),
    );
    await expect(
      createSource({ type: 'web', url: 'http://169.254.169.254/latest/meta-data' }),
    ).rejects.toMatchObject({ status: 422 });
  });

  it('POST /sources/{id}/sync re-syncs (202 → syncing)', async () => {
    const spy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(json({ ...sampleSource, status: 'syncing' }, 202));
    const synced = await syncSource(sampleSource.id);
    expect(synced.status).toBe('syncing');
    const { url, init } = lastCall(spy);
    expect(url).toContain(`/sources/${sampleSource.id}/sync`);
    expect(init.method).toBe('POST');
  });

  it('accepts a 200 from sync (already syncing — no-op)', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      json({ ...sampleSource, status: 'syncing' }, 200),
    );
    const synced = await syncSource(sampleSource.id);
    expect(synced.status).toBe('syncing');
  });

  it('DELETE /sources/{id} removes a source (204)', async () => {
    const spy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(null, { status: 204 }));
    await expect(deleteSource(sampleSource.id)).resolves.toBeUndefined();
    const { url, init } = lastCall(spy);
    expect(url).toContain(`/sources/${sampleSource.id}`);
    expect(init.method).toBe('DELETE');
  });

  it('missing/expired token → 401 ApiError (INV-4)', async () => {
    clearAccessToken();
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(problem(401, 'Unauthorized'));
    await expect(listSources()).rejects.toMatchObject({ status: 401 });
    await expect(listSources()).rejects.toBeInstanceOf(ApiError);
  });

  it('a non-owner / cross-tenant source on sync or delete → 404 (INV-1/INV-2)', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(problem(404, 'Not Found'));
    await expect(syncSource('00000000-0000-0000-0000-000000000000')).rejects.toMatchObject({
      status: 404,
    });
    await expect(deleteSource('00000000-0000-0000-0000-000000000000')).rejects.toBeInstanceOf(
      ApiError,
    );
  });
});
