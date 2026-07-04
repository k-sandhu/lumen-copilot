/**
 * Run-deliveries api/ boundary calls against a mocked fetch. Verifies the request
 * shapes conform to the frozen contract (contracts/openapi.yaml §run-deliveries,
 * #238) and that the spec-0004 negative categories surface as typed ApiErrors:
 *   - bad filter value → 422 (INV-8)
 *   - non-owned / cross-tenant / unknown id → 404 (INV-1/INV-2)
 *   - missing/expired token → 401 (INV-4)
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import {
  ApiError,
  clearAccessToken,
  listRunDeliveries,
  markRunDeliveryRead,
  setAccessToken,
} from '@/api';
import type { RunDelivery } from '@/api';

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

const DELIVERY: RunDelivery = {
  id: 'd1',
  run_id: 'r1',
  schedule_id: 's1',
  kind: 'inbox',
  status: 'delivered',
  summary: 'Run ready',
  created_at: '2026-07-03T00:00:00Z',
  read_at: null,
};

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

describe('run-deliveries api boundary', () => {
  it('GET /run-deliveries is bearer-authenticated and serializes filters + pagination', async () => {
    const spy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(json({ items: [] }));
    await listRunDeliveries({ status: 'delivered', unread: true, cursor: 'pg2', limit: 20 });
    const { url, init } = lastCall(spy);
    expect(url).toContain('/run-deliveries');
    expect(url).toContain('status=delivered');
    expect(url).toContain('unread=true');
    expect(url).toContain('cursor=pg2');
    expect(url).toContain('limit=20');
    expect(new Headers(init.headers).get('Authorization')).toBe('Bearer jwt');
  });

  it('omits unread=false so the default list is not spuriously filtered', async () => {
    const spy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(json({ items: [] }));
    await listRunDeliveries({ unread: false });
    expect(lastCall(spy).url).not.toContain('unread');
  });

  it('POST /run-deliveries/{id}/read marks one delivery read', async () => {
    const spy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(json({ ...DELIVERY, status: 'read', read_at: '2026-07-03T01:00:00Z' }));
    const res = await markRunDeliveryRead('d1');
    const { url, init } = lastCall(spy);
    expect(url).toContain('/run-deliveries/d1/read');
    expect(init.method).toBe('POST');
    expect(res.status).toBe('read');
    expect(res.read_at).not.toBeNull();
  });

  it('bad filter value → 422 ApiError (INV-8)', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(problem(422, 'Unprocessable'));
    await expect(listRunDeliveries()).rejects.toMatchObject({ status: 422 });
  });

  it('unknown / cross-tenant delivery → 404 ApiError (INV-1/INV-2)', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(problem(404, 'Not found'));
    await expect(markRunDeliveryRead('nope')).rejects.toBeInstanceOf(ApiError);
    await expect(markRunDeliveryRead('nope')).rejects.toMatchObject({ status: 404 });
  });

  it('missing/expired token → 401 ApiError (INV-4)', async () => {
    clearAccessToken();
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(problem(401, 'Unauthorized'));
    await expect(listRunDeliveries()).rejects.toMatchObject({ status: 401 });
  });
});
