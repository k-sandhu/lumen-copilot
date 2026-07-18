/**
 * Artifacts api/ boundary calls against a mocked fetch. Verifies the request
 * shape conforms to the frozen contract (contracts/openapi.yaml §artifacts, CC-B
 * #208 / #222) and that the spec-0004 negative categories surface as typed
 * ApiErrors:
 *   - non-owned / cross-tenant / unknown id → 404 (INV-1/INV-2)
 *   - malformed (non-uuid) id → 422 (INV-8)
 *   - missing/expired token → 401 (INV-4)
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import {
  ApiError,
  listArtifacts,
  getArtifact,
  deleteArtifact,
  fetchArtifactContent,
  setAccessToken,
  clearAccessToken,
} from '@/api';
import type { Artifact, ArtifactList } from '@/api';

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

const ART: Artifact = {
  id: 'art-1',
  filename: 'out.csv',
  mime_type: 'text/csv',
  size_bytes: 42,
  owner_id: 'u-1',
  produced_by: 'tool',
  created_at: '2026-07-02T00:00:00Z',
  session_id: null,
  run_id: null,
  tool_invocation_id: null,
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

describe('artifacts api boundary', () => {
  it('GET /artifacts is bearer-authenticated and lists a page', async () => {
    const page: ArtifactList = { items: [ART], next_cursor: null };
    const spy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(json(page));
    const res = await listArtifacts();
    const { url, init } = lastCall(spy);
    expect(url).toContain('/artifacts');
    expect(new Headers(init.headers).get('Authorization')).toBe('Bearer jwt');
    expect(res.items[0]?.id).toBe('art-1');
  });

  it('GET /artifacts forwards the session_id + produced_by filters', async () => {
    const spy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(json({ items: [], next_cursor: null } satisfies ArtifactList));
    await listArtifacts({ session_id: 's-9', produced_by: 'run' });
    const { url } = lastCall(spy);
    expect(url).toContain('session_id=s-9');
    expect(url).toContain('produced_by=run');
  });

  it('GET /artifacts/{id} fetches one artifact', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(json(ART));
    const res = await getArtifact('art-1');
    expect(res.filename).toBe('out.csv');
    expect(res.produced_by).toBe('tool');
  });

  it('DELETE /artifacts/{id} issues a DELETE', async () => {
    const spy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(new Response(null, { status: 204 }));
    await deleteArtifact('art-1');
    const { url, init } = lastCall(spy);
    expect(url).toContain('/artifacts/art-1');
    expect(init.method).toBe('DELETE');
  });

  it('GET /artifacts/{id}/content fetches bytes with the bearer and returns a blob URL', async () => {
    const createSpy = vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:artifact');
    const spy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(
        new Response(new Blob(['a,b\n1,2'], { type: 'text/csv' }), { status: 200 }),
      );
    const content = await fetchArtifactContent('art-1');
    const { url, init } = lastCall(spy);
    expect(url).toContain('/artifacts/art-1/content');
    expect(new Headers(init.headers).get('Authorization')).toBe('Bearer jwt');
    expect(init.redirect).toBe('follow');
    expect(content.url).toBe('blob:artifact');
    createSpy.mockRestore();
  });

  it('unknown / cross-tenant id → 404 ApiError (INV-1/INV-2)', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(problem(404, 'Not found'));
    await expect(getArtifact('nope')).rejects.toBeInstanceOf(ApiError);
    await expect(getArtifact('nope')).rejects.toMatchObject({ status: 404 });
  });

  it('malformed id → 422 ApiError (INV-8)', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(problem(422, 'Unprocessable'));
    await expect(getArtifact('not-a-uuid')).rejects.toMatchObject({ status: 422 });
  });

  it('missing/expired token → 401 ApiError (INV-4)', async () => {
    clearAccessToken();
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(problem(401, 'Unauthorized'));
    await expect(listArtifacts()).rejects.toMatchObject({ status: 401 });
  });
});
