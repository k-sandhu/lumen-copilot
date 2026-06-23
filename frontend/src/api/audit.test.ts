/**
 * Audit api/ boundary calls against a mocked fetch. Verifies the request shapes
 * conform to the frozen contract (contracts/openapi.yaml §audit, #80) and that
 * the spec-0004 negative categories surface as typed ApiErrors:
 *   - wrong role (non-admin/non-security) → 403 (INV-5)
 *   - missing/expired token → 401 (INV-4)
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { ApiError, listAuditEvents, setAccessToken, clearAccessToken } from '@/api';

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

describe('audit api boundary', () => {
  it('GET /audit is bearer-authenticated', async () => {
    const spy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(json({ items: [] }));
    await listAuditEvents();
    const { url, init } = lastCall(spy);
    expect(url).toContain('/audit');
    expect(new Headers(init.headers).get('Authorization')).toBe('Bearer jwt');
  });

  it('serializes actor / event_type / resource_id / from / to + pagination', async () => {
    const spy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(json({ items: [] }));
    await listAuditEvents({
      actor: 'user-1',
      event_type: 'permission.denied',
      resource_id: 'doc-9',
      from: '2026-06-01T00:00:00Z',
      to: '2026-06-19T00:00:00Z',
      cursor: 'pg2',
      limit: 50,
    });
    const { url } = lastCall(spy);
    expect(url).toContain('actor=user-1');
    expect(url).toContain('event_type=permission.denied');
    expect(url).toContain('resource_id=doc-9');
    expect(url).toContain('from=');
    expect(url).toContain('to=');
    expect(url).toContain('cursor=pg2');
    expect(url).toContain('limit=50');
  });

  it('parses events with provenance (candidate allow/exclude dispositions)', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      json({
        items: [
          {
            id: 'a1',
            ts: '2026-06-19T10:00:00Z',
            actor: 'user-1',
            tenant_id: 't1',
            event_type: 'retrieval.query',
            resource_id: null,
            decision: 'allowed',
            provenance: {
              candidates: [
                { resource_id: 'p1', disposition: 'allow', reason: 'in allow-set' },
                { resource_id: 'p2', disposition: 'exclude', reason: 'owner mismatch' },
              ],
              raw: { query_hash: 'abc' },
            },
          },
        ],
        next_cursor: null,
      }),
    );
    const res = await listAuditEvents();
    expect(res.items[0]?.provenance.candidates).toHaveLength(2);
    expect(res.items[0]?.provenance.candidates[1]?.disposition).toBe('exclude');
  });

  it('non-admin/non-security caller → 403 ApiError (INV-5)', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(problem(403, 'Forbidden'));
    await expect(listAuditEvents()).rejects.toMatchObject({ status: 403 });
    await expect(listAuditEvents()).rejects.toBeInstanceOf(ApiError);
  });

  it('missing/expired token → 401 ApiError (INV-4)', async () => {
    clearAccessToken();
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(problem(401, 'Unauthorized'));
    await expect(listAuditEvents()).rejects.toMatchObject({ status: 401 });
  });
});
