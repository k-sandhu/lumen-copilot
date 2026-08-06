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
  attestMemberIdentity,
  getModelGovernance,
  getRiskTiers,
  getToolPolicy,
  updateToolPolicy,
  listGroups,
  createGroup,
  getGroup,
  renameGroup,
  deleteGroup,
  listGroupMembers,
  addGroupMember,
  removeGroupMember,
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

  it('parses a member roster (id, email, role[], email_attested_at)', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      json({
        items: [{ id: 'u1', email: 'a@x.test', role: ['admin'], email_attested_at: null }],
        next_cursor: null,
      }),
    );
    const res = await listMembers();
    expect(res.items[0]?.email).toBe('a@x.test');
    expect(res.items[0]?.role).toContain('admin');
    expect(res.items[0]?.email_attested_at).toBeNull();
  });

  it('POST /admin/members/{id}/attest-identity returns the member with the new attestation (ADR-0019 §2)', async () => {
    const spy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      json({
        id: 'u1',
        email: 'a@x.test',
        role: ['member'],
        email_attested_at: '2026-07-18T12:00:00Z',
      }),
    );
    const res = await attestMemberIdentity('u1');
    expect(res.email_attested_at).toBe('2026-07-18T12:00:00Z');
    const { url, init } = lastCall(spy);
    expect(url).toContain('/admin/members/u1/attest-identity');
    expect(init.method).toBe('POST');
    expect(new Headers(init.headers).get('Authorization')).toBe('Bearer jwt');
  });

  it('a non-admin attest → 403 ApiError (INV-5); a cross-tenant member → 404 (INV-1)', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(problem(403, 'Forbidden'));
    await expect(attestMemberIdentity('u1')).rejects.toMatchObject({ status: 403 });
    vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(problem(404, 'Not Found'));
    await expect(attestMemberIdentity('ghost')).rejects.toBeInstanceOf(ApiError);
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

  it('GET /admin/tool-policy returns one entry per registered tool (#223)', async () => {
    const spy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      json({
        items: [
          {
            tool_name: 'run_python',
            risk_tier: 'T2',
            read_only: false,
            enabled: true,
            requires_approval: true,
            is_default: true,
          },
        ],
      }),
    );
    const res = await getToolPolicy();
    expect(lastCall(spy).url).toContain('/admin/tool-policy');
    expect(res.items[0]?.tool_name).toBe('run_python');
    expect(res.items[0]?.risk_tier).toBe('T2');
  });

  it('PATCH /admin/tool-policy sends both flags + the tool name (#223)', async () => {
    const spy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(json({ items: [] }));
    await updateToolPolicy({ tool_name: 'run_python', enabled: true, requires_approval: false });
    const { url, init } = lastCall(spy);
    expect(url).toContain('/admin/tool-policy');
    expect(init.method).toBe('PATCH');
    expect(JSON.parse(String(init.body))).toEqual({
      tool_name: 'run_python',
      enabled: true,
      requires_approval: false,
    });
  });

  it('an unknown tool name → 422 ApiError (INV-8)', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(problem(422, 'Unprocessable Entity'));
    await expect(
      updateToolPolicy({ tool_name: 'nope', enabled: true, requires_approval: false }),
    ).rejects.toMatchObject({ status: 422 });
  });

  it('non-admin caller → 403 ApiError on every admin surface (INV-5)', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(problem(403, 'Forbidden'));
    await expect(listMembers()).rejects.toMatchObject({ status: 403 });
    await expect(getModelGovernance()).rejects.toBeInstanceOf(ApiError);
    await expect(getRiskTiers()).rejects.toMatchObject({ status: 403 });
    await expect(getToolPolicy()).rejects.toMatchObject({ status: 403 });
    await expect(
      updateToolPolicy({ tool_name: 'run_python', enabled: true, requires_approval: false }),
    ).rejects.toMatchObject({ status: 403 });
  });
});

/**
 * Groups (#540, ADR-0022) — the admin-gated group CRUD + membership surface.
 * These pin the request SHAPES against the frozen contract (paths, methods,
 * bodies) and the negative paths the panel branches on: 409 by RFC-9457 `code`,
 * 404 for a cross-tenant group (INV-1, never 403), 403 for a non-admin (INV-5).
 */
describe('admin groups api boundary', () => {
  const GROUP = {
    id: 'g1',
    name: 'Tax Team',
    kind: 'user',
    member_count: 2,
    created_at: '2026-07-30T00:00:00Z',
    updated_at: '2026-07-30T00:00:00Z',
  };

  it('GET /admin/groups is unpaginated + bearer-authenticated', async () => {
    const spy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(json({ items: [GROUP] }));
    const result = await listGroups();
    const { url, init } = lastCall(spy);
    expect(url).toContain('/admin/groups');
    expect(url).not.toContain('?');
    expect(init.method).toBeUndefined();
    expect(new Headers(init.headers).get('Authorization')).toBe('Bearer jwt');
    expect(result.items[0]?.name).toBe('Tax Team');
  });

  it('parses the system group’s null member_count as null, not 0', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      json({ items: [{ ...GROUP, kind: 'system', name: 'All members', member_count: null }] }),
    );
    const result = await listGroups();
    expect(result.items[0]?.member_count).toBeNull();
  });

  it('POST /admin/groups sends the name', async () => {
    const spy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(json(GROUP, 201));
    await createGroup({ name: 'Tax Team' });
    const { url, init } = lastCall(spy);
    expect(url).toContain('/admin/groups');
    expect(init.method).toBe('POST');
    expect(JSON.parse(String(init.body))).toEqual({ name: 'Tax Team' });
  });

  it('GET /admin/groups/{id} reads one group', async () => {
    const spy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(json(GROUP));
    await getGroup('g1');
    expect(lastCall(spy).url).toContain('/admin/groups/g1');
  });

  it('PATCH /admin/groups/{id} renames', async () => {
    const spy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(json(GROUP));
    await renameGroup('g1', { name: 'Tax & Treasury' });
    const { url, init } = lastCall(spy);
    expect(url).toContain('/admin/groups/g1');
    expect(init.method).toBe('PATCH');
    expect(JSON.parse(String(init.body))).toEqual({ name: 'Tax & Treasury' });
  });

  it('DELETE /admin/groups/{id} resolves on 204', async () => {
    const spy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(null, { status: 204 }));
    await expect(deleteGroup('g1')).resolves.toBeUndefined();
    const { url, init } = lastCall(spy);
    expect(url).toContain('/admin/groups/g1');
    expect(init.method).toBe('DELETE');
  });

  it('GET /admin/groups/{id}/members lists the roster', async () => {
    const spy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      json({ items: [{ id: 'u1', email: 'ada@acme.test', role: ['member'], email_attested_at: null }] }),
    );
    const result = await listGroupMembers('g1');
    expect(lastCall(spy).url).toContain('/admin/groups/g1/members');
    expect(result.items[0]?.email).toBe('ada@acme.test');
  });

  it('POST /admin/groups/{id}/members sends user_id and resolves on 204', async () => {
    const spy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(null, { status: 204 }));
    await expect(addGroupMember('g1', { user_id: 'u1' })).resolves.toBeUndefined();
    const { url, init } = lastCall(spy);
    expect(url).toContain('/admin/groups/g1/members');
    expect(init.method).toBe('POST');
    expect(JSON.parse(String(init.body))).toEqual({ user_id: 'u1' });
  });

  it('DELETE /admin/groups/{id}/members/{userId} resolves on 204', async () => {
    const spy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(null, { status: 204 }));
    await expect(removeGroupMember('g1', 'u1')).resolves.toBeUndefined();
    const { url, init } = lastCall(spy);
    expect(url).toContain('/admin/groups/g1/members/u1');
    expect(init.method).toBe('DELETE');
  });

  it('a 409 carries the machine-readable code the UI branches on', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(
        JSON.stringify({
          type: 'about:blank',
          title: 'Conflict',
          status: 409,
          code: 'group_name_taken',
        }),
        { status: 409, headers: { 'Content-Type': 'application/problem+json' } },
      ),
    );
    await expect(createGroup({ name: 'Tax Team' })).rejects.toMatchObject({
      status: 409,
      problem: { code: 'group_name_taken' },
    });
  });

  it('a cross-tenant group is 404, never 403 (INV-1)', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(problem(404, 'Not Found'));
    await expect(getGroup('other-tenant-group')).rejects.toMatchObject({ status: 404 });
  });

  it('non-admin caller → 403 ApiError on every group surface (INV-5)', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(problem(403, 'Forbidden'));
    await expect(listGroups()).rejects.toMatchObject({ status: 403 });
    await expect(createGroup({ name: 'x' })).rejects.toBeInstanceOf(ApiError);
    await expect(renameGroup('g1', { name: 'x' })).rejects.toMatchObject({ status: 403 });
    await expect(deleteGroup('g1')).rejects.toMatchObject({ status: 403 });
    await expect(listGroupMembers('g1')).rejects.toMatchObject({ status: 403 });
    await expect(addGroupMember('g1', { user_id: 'u1' })).rejects.toMatchObject({ status: 403 });
    await expect(removeGroupMember('g1', 'u1')).rejects.toMatchObject({ status: 403 });
  });
});
