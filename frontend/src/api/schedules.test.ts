/**
 * Schedules api/ boundary calls against a mocked fetch. Verifies the request
 * shapes conform to the frozen contract (contracts/openapi.yaml §schedules, #234)
 * and that the spec-0004 negative categories surface as typed ApiErrors:
 *   - malformed cron / IANA timezone → 422 (INV-8)
 *   - non-owned / cross-tenant / unknown id → 404 (INV-1/INV-2)
 *   - run-now against an unavailable assistant / illegal state → 409 (INV-8)
 *   - missing/expired token → 401 (INV-4)
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import {
  ApiError,
  createSchedule,
  deleteSchedule,
  getSchedule,
  listSchedules,
  pauseSchedule,
  resumeSchedule,
  runScheduleNow,
  setAccessToken,
  clearAccessToken,
  updateSchedule,
} from '@/api';
import type { Schedule } from '@/api';

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

const SCHEDULE: Schedule = {
  id: 's1',
  assistant_id: 'a1',
  owner_id: 'u1',
  cadence: { cron: '0 8 * * 1' },
  timezone: 'America/New_York',
  delivery: { inbox: true },
  overlap_policy: 'skip',
  enabled: true,
  next_run_at: '2026-07-06T12:00:00Z',
  last_run_at: null,
  last_status: null,
  created_at: '2026-07-02T00:00:00Z',
  updated_at: '2026-07-02T00:00:00Z',
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

describe('schedules api boundary', () => {
  it('GET /schedules is bearer-authenticated and serializes filters + pagination', async () => {
    const spy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(json({ items: [] }));
    await listSchedules({ assistant_id: 'a1', enabled: false, cursor: 'pg2', limit: 20 });
    const { url, init } = lastCall(spy);
    expect(url).toContain('/schedules');
    expect(url).toContain('assistant_id=a1');
    expect(url).toContain('enabled=false');
    expect(url).toContain('cursor=pg2');
    expect(url).toContain('limit=20');
    expect(new Headers(init.headers).get('Authorization')).toBe('Bearer jwt');
  });

  it('POST /schedules sends the create body', async () => {
    const spy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(json(SCHEDULE, 201));
    await createSchedule({
      assistant_id: 'a1',
      cadence: { cron: '0 8 * * 1' },
      timezone: 'America/New_York',
    });
    const { url, init } = lastCall(spy);
    expect(url).toContain('/schedules');
    expect(init.method).toBe('POST');
    expect(JSON.parse(String(init.body))).toMatchObject({
      assistant_id: 'a1',
      timezone: 'America/New_York',
    });
  });

  it('GET /schedules/{id} fetches one schedule', async () => {
    const spy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(json(SCHEDULE));
    const res = await getSchedule('s1');
    const { url } = lastCall(spy);
    expect(url).toContain('/schedules/s1');
    expect(res.id).toBe('s1');
  });

  it('PATCH /schedules/{id} sends the update body', async () => {
    const spy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(json(SCHEDULE));
    await updateSchedule('s1', { timezone: 'UTC' });
    const { url, init } = lastCall(spy);
    expect(url).toContain('/schedules/s1');
    expect(init.method).toBe('PATCH');
    expect(JSON.parse(String(init.body))).toEqual({ timezone: 'UTC' });
  });

  it('DELETE /schedules/{id} issues a DELETE', async () => {
    const spy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(new Response(null, { status: 204 }));
    await deleteSchedule('s1');
    const { url, init } = lastCall(spy);
    expect(url).toContain('/schedules/s1');
    expect(init.method).toBe('DELETE');
  });

  it('POST /schedules/{id}/pause and /resume hit the lifecycle endpoints', async () => {
    const spy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(json({ ...SCHEDULE, enabled: false, next_run_at: null }));
    await pauseSchedule('s1');
    expect(lastCall(spy).url).toContain('/schedules/s1/pause');
    expect(lastCall(spy).init.method).toBe('POST');

    spy.mockResolvedValue(json(SCHEDULE));
    await resumeSchedule('s1');
    expect(lastCall(spy).url).toContain('/schedules/s1/resume');
  });

  it('POST /schedules/{id}/run-now accepts the 202 and returns the run id', async () => {
    const spy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(json({ run_id: 'r99' }, 202));
    const res = await runScheduleNow('s1');
    expect(lastCall(spy).url).toContain('/schedules/s1/run-now');
    expect(res.run_id).toBe('r99');
  });

  it('malformed cron → 422 ApiError carrying invalid_cron code (INV-8)', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(problem(422, 'Invalid cron', 'invalid_cron'));
    await expect(
      createSchedule({ assistant_id: 'a1', cadence: { cron: 'nope' }, timezone: 'UTC' }),
    ).rejects.toMatchObject({ status: 422 });
  });

  it('unknown / cross-tenant schedule → 404 ApiError (INV-1/INV-2)', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(problem(404, 'Not found'));
    await expect(getSchedule('nope')).rejects.toBeInstanceOf(ApiError);
    await expect(getSchedule('nope')).rejects.toMatchObject({ status: 404 });
  });

  it('run-now against an unavailable assistant / illegal state → 409 (INV-8)', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(problem(409, 'Conflict'));
    await expect(runScheduleNow('s1')).rejects.toMatchObject({ status: 409 });
  });

  it('missing/expired token → 401 ApiError (INV-4)', async () => {
    clearAccessToken();
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(problem(401, 'Unauthorized'));
    await expect(listSchedules()).rejects.toMatchObject({ status: 401 });
  });
});
