/**
 * Runs api/ boundary calls against a mocked fetch. Verifies the request shapes
 * conform to the frozen contract (contracts/openapi.yaml §runs, #234) and that the
 * spec-0004 negative categories surface as typed ApiErrors:
 *   - bad filter value → 422 (INV-8)
 *   - non-owned / cross-tenant / unknown id → 404 (INV-1/INV-2)
 *   - missing/expired token → 401 (INV-4)
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import {
  ApiError,
  cancelRun,
  getRun,
  listRuns,
  rerouteRun,
  resumeRun,
  setAccessToken,
  clearAccessToken,
} from '@/api';
import type { Run } from '@/api';

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

const RUN: Run = {
  id: 'r1',
  schedule_id: 's1',
  assistant_id: 'a1',
  trigger: 'schedule',
  status: 'succeeded',
  summary: 'All good',
  created_at: '2026-07-02T00:00:00Z',
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

describe('runs api boundary', () => {
  it('GET /runs is bearer-authenticated and serializes filters + pagination', async () => {
    const spy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(json({ items: [] }));
    await listRuns({ assistant_id: 'a1', schedule_id: 's1', status: 'failed', cursor: 'pg2', limit: 20 });
    const { url, init } = lastCall(spy);
    expect(url).toContain('/runs');
    expect(url).toContain('assistant_id=a1');
    expect(url).toContain('schedule_id=s1');
    expect(url).toContain('status=failed');
    expect(url).toContain('cursor=pg2');
    expect(url).toContain('limit=20');
    expect(new Headers(init.headers).get('Authorization')).toBe('Bearer jwt');
  });

  it('GET /runs/{id} fetches one run detail', async () => {
    const spy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(json({ ...RUN, steps: [], citations: [] }));
    const res = await getRun('r1');
    expect(lastCall(spy).url).toContain('/runs/r1');
    expect(res.id).toBe('r1');
    expect(res.status).toBe('succeeded');
  });

  it('bad filter value → 422 ApiError (INV-8)', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(problem(422, 'Unprocessable'));
    await expect(listRuns()).rejects.toMatchObject({ status: 422 });
  });

  it('unknown / cross-tenant run → 404 ApiError (INV-1/INV-2)', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(problem(404, 'Not found'));
    await expect(getRun('nope')).rejects.toBeInstanceOf(ApiError);
    await expect(getRun('nope')).rejects.toMatchObject({ status: 404 });
  });

  it('missing/expired token → 401 ApiError (INV-4)', async () => {
    clearAccessToken();
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(problem(401, 'Unauthorized'));
    await expect(listRuns()).rejects.toMatchObject({ status: 401 });
  });

  // --- Escalation handoff (E7-5, #239) --------------------------------------

  it('POST /runs/{id}/resume re-enqueues an escalated run (202)', async () => {
    const spy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(json({ ...RUN, status: 'queued' }, 202));
    const res = await resumeRun('r1');
    const { url, init } = lastCall(spy);
    expect(url).toContain('/runs/r1/resume');
    expect(init.method).toBe('POST');
    expect(res.status).toBe('queued');
  });

  it('POST /runs/{id}/cancel closes an escalated run', async () => {
    const spy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(json({ ...RUN, status: 'failed', error: { code: 'cancelled', message: 'x' } }));
    const res = await cancelRun('r1');
    expect(lastCall(spy).url).toContain('/runs/r1/cancel');
    expect(res.error?.code).toBe('cancelled');
  });

  it('POST /runs/{id}/reroute sends the target owner (202)', async () => {
    const spy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(json({ ...RUN, status: 'queued' }, 202));
    await rerouteRun('r1', { to_owner_id: 'u2' });
    const { url, init } = lastCall(spy);
    expect(url).toContain('/runs/r1/reroute');
    expect(init.method).toBe('POST');
    expect(JSON.parse(String(init.body))).toEqual({ to_owner_id: 'u2' });
  });

  it('resume of a non-escalated run → 409 ApiError (INV-8)', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(problem(409, 'Conflict'));
    await expect(resumeRun('r1')).rejects.toMatchObject({ status: 409 });
  });

  it('reroute to an unknown owner → 404 ApiError (INV-1)', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(problem(404, 'Not found'));
    await expect(rerouteRun('r1', { to_owner_id: 'ghost' })).rejects.toBeInstanceOf(ApiError);
  });
});
