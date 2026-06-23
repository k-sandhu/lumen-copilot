/**
 * Admin api/ boundary calls against a mocked fetch. Verifies the request shapes
 * conform to the frozen contract (contracts/openapi.yaml §admin, #80) and that a
 * non-admin caller is rejected with a typed 403 ApiError (INV-5). All three are
 * read-only governance surfaces.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import {
  ApiError,
  listMembers,
  getModelGovernance,
  getRiskTiers,
  setAccessToken,
  clearAccessToken,
} from '@/api';

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

describe('admin api boundary', () => {
  it('GET /admin/members paginates + is bearer-authenticated', async () => {
    const spy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(json({ items: [] }));
    await listMembers({ cursor: 'c', limit: 10 });
    const { url, init } = lastCall(spy);
    expect(url).toContain('/admin/members');
    expect(url).toContain('cursor=c');
    expect(url).toContain('limit=10');
    expect(new Headers(init.headers).get('Authorization')).toBe('Bearer jwt');
  });

  it('parses a member roster (id, email, role[])', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      json({
        items: [{ id: 'u1', email: 'a@x.test', role: ['admin'] }],
        next_cursor: null,
      }),
    );
    const res = await listMembers();
    expect(res.items[0]?.email).toBe('a@x.test');
    expect(res.items[0]?.role).toContain('admin');
  });

  it('GET /admin/model-governance returns allowed_models + tiers', async () => {
    const spy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      json({
        allowed_models: [{ model_id: 'anthropic/claude-opus-4.8', tier: 'frontier' }],
        tiers: [{ id: 'frontier', description: 'Highest-capability tier.' }],
      }),
    );
    const res = await getModelGovernance();
    expect(lastCall(spy).url).toContain('/admin/model-governance');
    expect(res.allowed_models[0]?.tier).toBe('frontier');
    expect(res.tiers[0]?.id).toBe('frontier');
  });

  it('GET /admin/risk-tiers returns T0–T3 with approval', async () => {
    const spy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      json({
        items: [
          { tier: 'T0', description: 'Read-only.', approval: 'none' },
          { tier: 'T3', description: 'Destructive external.', approval: 'human approval + risk tier' },
        ],
      }),
    );
    const res = await getRiskTiers();
    expect(lastCall(spy).url).toContain('/admin/risk-tiers');
    expect(res.items.map((t) => t.tier)).toEqual(['T0', 'T3']);
  });

  it('non-admin caller → 403 ApiError on every admin surface (INV-5)', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(problem(403, 'Forbidden'));
    await expect(listMembers()).rejects.toMatchObject({ status: 403 });
    await expect(getModelGovernance()).rejects.toBeInstanceOf(ApiError);
    await expect(getRiskTiers()).rejects.toMatchObject({ status: 403 });
  });
});
