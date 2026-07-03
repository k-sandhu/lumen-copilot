/**
 * Code-runs api/ boundary calls against a mocked fetch. Verifies the request
 * shape conforms to the frozen contract (contracts/openapi.yaml §code-runs, #229)
 * and that the spec-0004 negative categories surface as typed ApiErrors:
 *   - non-owned / cross-tenant / unknown id → 404 (INV-1/INV-2)
 *   - malformed (non-uuid) id → 422 (INV-8)
 *   - missing/expired token → 401 (INV-4)
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { ApiError, getCodeRun, setAccessToken, clearAccessToken } from '@/api';
import type { CodeRun } from '@/api';

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

const RUN: CodeRun = {
  id: 'cr-1',
  status: 'succeeded',
  code: 'print("hi")',
  stdout: 'hi\n',
  stderr: '',
  exit_code: 0,
  duration_ms: 120,
  resource_usage: { peak_memory_bytes: 1024, cpu_time_ms: 40, max_pids: 2, output_bytes: 3 },
  artifact_ids: ['art-1'],
  image_digest: 'sha256:abc',
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

describe('code-runs api boundary', () => {
  it('GET /code-runs/{id} is bearer-authenticated and fetches one run', async () => {
    const spy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(json(RUN));
    const res = await getCodeRun('cr-1');
    const { url, init } = lastCall(spy);
    expect(url).toContain('/code-runs/cr-1');
    expect(new Headers(init.headers).get('Authorization')).toBe('Bearer jwt');
    expect(res.id).toBe('cr-1');
    expect(res.status).toBe('succeeded');
    expect(res.artifact_ids).toEqual(['art-1']);
  });

  it('unknown / cross-tenant run → 404 ApiError (INV-1/INV-2)', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(problem(404, 'Not found'));
    await expect(getCodeRun('nope')).rejects.toBeInstanceOf(ApiError);
    await expect(getCodeRun('nope')).rejects.toMatchObject({ status: 404 });
  });

  it('malformed id → 422 ApiError (INV-8)', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(problem(422, 'Unprocessable'));
    await expect(getCodeRun('not-a-uuid')).rejects.toMatchObject({ status: 422 });
  });

  it('missing/expired token → 401 ApiError (INV-4)', async () => {
    clearAccessToken();
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(problem(401, 'Unauthorized'));
    await expect(getCodeRun('cr-1')).rejects.toMatchObject({ status: 401 });
  });
});
