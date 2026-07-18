/**
 * Sources api/ boundary calls against a mocked fetch. Verifies the request shapes
 * conform to the frozen contract (contracts/openapi.yaml §sources, ADR-0009 /
 * #108 + ADR-0019 §1 / #451) and that the spec-0004 negative categories surface
 * as typed ApiErrors the UI branches on:
 *   - missing/expired token → 401 (INV-4)
 *   - invalid / SSRF-blocked URL on add → 422 (INV-8, ADR-0009 §3)
 *   - non-owner / cross-tenant source on sync/delete → 404 (INV-1/INV-2)
 *   - non-admin managed-source mutation → 403 (INV-5, ADR-0019 §1)
 *   - connect on a `web` source → 409 oauth_not_supported (INV-8)
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import {
  ApiError,
  listSources,
  createSource,
  connectSource,
  syncSource,
  deleteSource,
  setAccessToken,
  clearAccessToken,
} from '@/api';
import type { GdriveSource, Source } from '@/api';

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

const sampleGdrive: GdriveSource = {
  id: '33333333-3333-3333-3333-333333333333',
  type: 'gdrive',
  config: { mode: 'shared_drive', drive_id: '0AbCd' },
  status: 'pending_auth',
  indexed_count: 0,
  last_synced_at: null,
  connected_account: null,
  acl_synced_at: null,
  unmapped_acl_count: null,
  reauthorize_required: false,
  owner_id: '22222222-2222-2222-2222-222222222222',
  created_at: '2026-07-18T00:00:00Z',
  updated_at: '2026-07-18T00:00:00Z',
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

  it('POST /sources adds a gdrive source ({type, config}) → pending_auth with the health surface', async () => {
    const spy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(json(sampleGdrive, 201));
    const created = await createSource({
      type: 'gdrive',
      config: { mode: 'shared_drive', drive_id: '0AbCd' },
    });
    expect(created.status).toBe('pending_auth');
    // The managed health surface is REQUIRED on the gdrive branch (nullable).
    expect(created.type).toBe('gdrive');
    if (created.type === 'gdrive') {
      expect(created.connected_account).toBeNull();
      expect(created.acl_synced_at).toBeNull();
      expect(created.unmapped_acl_count).toBeNull();
      expect(created.reauthorize_required).toBe(false);
    }
    const { init } = lastCall(spy);
    expect(JSON.parse(init.body as string)).toEqual({
      type: 'gdrive',
      config: { mode: 'shared_drive', drive_id: '0AbCd' },
    });
  });

  it('a non-admin gdrive add → 403 ApiError (INV-5, ADR-0019 §1)', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(problem(403, 'Forbidden'));
    await expect(
      createSource({ type: 'gdrive', config: { mode: 'my_drive' } }),
    ).rejects.toMatchObject({ status: 403 });
  });

  it('an invalid gdrive config → 422 ApiError code=invalid_config (INV-8)', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      problem(422, 'Unprocessable Entity', 'invalid_config'),
    );
    await expect(
      createSource({ type: 'gdrive', config: { mode: 'folder', folder_id: '' } }),
    ).rejects.toMatchObject({ status: 422 });
  });

  it('POST /sources/{id}/connect returns the consent authorization_url', async () => {
    const spy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      json({ authorization_url: 'https://accounts.google.com/o/oauth2/v2/auth?state=opaque' }),
    );
    const res = await connectSource(sampleGdrive.id);
    expect(res.authorization_url).toBe(
      'https://accounts.google.com/o/oauth2/v2/auth?state=opaque',
    );
    const { url, init } = lastCall(spy);
    expect(url).toContain(`/sources/${sampleGdrive.id}/connect`);
    expect(init.method).toBe('POST');
    expect(new Headers(init.headers).get('Authorization')).toBe('Bearer jwt');
  });

  it('a non-admin connect → 403 ApiError (INV-5)', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(problem(403, 'Forbidden'));
    await expect(connectSource(sampleGdrive.id)).rejects.toMatchObject({ status: 403 });
    await expect(connectSource(sampleGdrive.id)).rejects.toBeInstanceOf(ApiError);
  });

  it('connect on a web source → 409 ApiError code=oauth_not_supported (INV-8)', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      problem(409, 'Conflict', 'oauth_not_supported'),
    );
    await expect(connectSource(sampleSource.id)).rejects.toMatchObject({ status: 409 });
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
