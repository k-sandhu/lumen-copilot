import { describe, it, expect, vi, afterEach } from 'vitest';
import { request, ApiError } from './client';

function jsonResponse(body: unknown, status = 200, contentType = 'application/json'): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': contentType },
  });
}

afterEach(() => vi.restoreAllMocks());

describe('request', () => {
  it('parses a JSON success body', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse({ status: 'ok' }));
    const data = await request<{ status: string }>('/health');
    expect(data.status).toBe('ok');
  });

  it('throws ApiError with the parsed Problem body on failure', async () => {
    // A Response body can only be read once; this test calls request() twice, so
    // return a FRESH Response per fetch invocation (not one shared instance).
    vi.spyOn(globalThis, 'fetch').mockImplementation(async () =>
      jsonResponse(
        { type: 'about:blank', title: 'Service Unavailable', status: 503, detail: 'db down' },
        503,
        'application/problem+json',
      ),
    );

    await expect(request('/health/ready')).rejects.toMatchObject({
      name: 'ApiError',
      status: 503,
    });

    try {
      await request('/health/ready');
    } catch (err) {
      expect(err).toBeInstanceOf(ApiError);
      if (err instanceof ApiError) {
        expect(err.problem?.title).toBe('Service Unavailable');
        expect(err.displayMessage).toBe('db down');
      }
    }
  });

  it('accepts whitelisted non-2xx statuses (e.g. 503 readiness)', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      jsonResponse({ status: 'degraded', dependencies: [] }, 503),
    );
    const data = await request<{ status: string }>('/health/ready', { okStatuses: [503] });
    expect(data.status).toBe('degraded');
  });

  it('wraps network failures as ApiError with status 0', async () => {
    vi.spyOn(globalThis, 'fetch').mockRejectedValue(new TypeError('Failed to fetch'));
    await expect(request('/health')).rejects.toMatchObject({ name: 'ApiError', status: 0 });
  });
});
