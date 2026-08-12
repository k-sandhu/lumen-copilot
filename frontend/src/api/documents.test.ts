/** Typed document control-plane calls, including v2 signed access/transcripts. */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  ApiError,
  createCollection,
  createDocumentAccessUrl,
  deleteCollection,
  deleteDocument,
  fetchDocumentContent,
  fetchDocumentText,
  fetchDocumentTranscript,
  getDocument,
  listCollections,
  listDocuments,
  registerRefreshHandler,
  updateCollection,
} from './index';
import { clearAccessToken, setAccessToken } from './token';

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function problem(status: number, title: string, detail?: string, code?: string): Response {
  return new Response(JSON.stringify({ type: 'about:blank', title, status, detail, code }), {
    status,
    headers: { 'Content-Type': 'application/problem+json' },
  });
}

const collection = {
  id: '11111111-1111-4111-8111-111111111111',
  name: 'Acme contracts',
  owner_id: '22222222-2222-4222-8222-222222222222',
  document_count: 2,
  created_at: '2026-06-18T00:00:00Z',
  updated_at: '2026-06-18T00:00:00Z',
};

const document = {
  id: '33333333-3333-4333-8333-333333333333',
  filename: 'meeting.mp3',
  mime_type: 'audio/mpeg',
  size_bytes: 1024,
  collection_id: collection.id,
  owner_id: collection.owner_id,
  kind: 'audio',
  duration_ms: 60_000,
  status: 'ready',
  chunk_count: 7,
  created_at: '2026-06-18T00:00:00Z',
  updated_at: '2026-06-18T00:00:00Z',
};

beforeEach(() => {
  clearAccessToken();
  registerRefreshHandler(null);
});

afterEach(() => {
  vi.restoreAllMocks();
  registerRefreshHandler(null);
  clearAccessToken();
});

describe('collections api', () => {
  it('lists, creates, updates and deletes collections', async () => {
    const fetchSpy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(json({ items: [collection], next_cursor: null }))
      .mockResolvedValueOnce(json(collection, 201))
      .mockResolvedValueOnce(json({ ...collection, name: 'Renamed' }))
      .mockResolvedValueOnce(new Response(null, { status: 204 }));

    expect((await listCollections({ cursor: 'next', limit: 20 })).items).toHaveLength(1);
    expect(await createCollection({ name: collection.name })).toMatchObject({ id: collection.id });
    expect(await updateCollection(collection.id, { name: 'Renamed' })).toMatchObject({
      name: 'Renamed',
    });
    await expect(deleteCollection(collection.id)).resolves.toBeUndefined();
    expect(String(fetchSpy.mock.calls[0]?.[0])).toContain('cursor=next');
  });

  it('preserves typed validation/tenancy failures', async () => {
    vi.spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(problem(422, 'Unprocessable Entity'))
      .mockResolvedValueOnce(problem(404, 'Not Found'));
    await expect(createCollection({ name: '' })).rejects.toMatchObject({ status: 422 });
    await expect(deleteCollection('missing')).rejects.toMatchObject({ status: 404 });
  });
});

describe('documents metadata/text api', () => {
  it('lists with filters, gets and deletes documents', async () => {
    const fetchSpy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(json({ items: [document], next_cursor: null }))
      .mockResolvedValueOnce(json(document))
      .mockResolvedValueOnce(new Response(null, { status: 204 }));
    await listDocuments({ collection_id: collection.id, status: 'ready', q: 'meeting' });
    expect(String(fetchSpy.mock.calls[0]?.[0])).toContain('status=ready');
    expect(await getDocument(document.id)).toMatchObject({ kind: 'audio' });
    await expect(deleteDocument(document.id)).resolves.toBeUndefined();
  });

  it('returns extracted text and preserves 404/409 failures', async () => {
    vi.spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(json({ text: 'body', chunk_count: 1, truncated: false }))
      .mockResolvedValueOnce(problem(404, 'Not Found'))
      .mockResolvedValueOnce(problem(409, 'Conflict', 'not ready', 'document_not_ready'));
    expect(await fetchDocumentText(document.id)).toMatchObject({ text: 'body' });
    await expect(fetchDocumentText('hidden')).rejects.toMatchObject({ status: 404 });
    await expect(fetchDocumentText(document.id)).rejects.toMatchObject({ status: 409 });
  });

  it('maps a hidden direct read to ApiError 404', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(problem(404, 'Not Found'));
    await expect(getDocument('hidden')).rejects.toBeInstanceOf(ApiError);
  });
});

describe('v2 signed access + transcript', () => {
  const access = {
    url: 'https://storage.example/object?signed',
    filename: 'meeting.mp3',
    mime_type: 'audio/mpeg',
    size_bytes: 1024,
    expires_at: '2026-06-18T01:00:00Z',
    purpose: 'preview',
    supports_byte_ranges: true,
  };

  it('mints a JSON capability and never fetches the object bytes', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(json(access));
    const result = await createDocumentAccessUrl(document.id, 'preview');

    expect(result.url).toBe(access.url);
    expect(fetchSpy).toHaveBeenCalledTimes(1);
    expect(String(fetchSpy.mock.calls[0]?.[0])).toContain(
      `/api/v2/documents/${document.id}/access-url`,
    );
    expect(String(fetchSpy.mock.calls[0]?.[0])).not.toContain('/api/v1/v2/');
    const init = fetchSpy.mock.calls[0]?.[1] as RequestInit;
    expect(init.method).toBe('POST');
    expect(JSON.parse(String(init.body))).toEqual({ purpose: 'preview' });
  });

  it('the compatibility content helper returns the signed URL without a blob allocation', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(json(access));
    const createObjectUrl = vi.spyOn(URL, 'createObjectURL');
    const result = await fetchDocumentContent(document.id);
    expect(result).toMatchObject({
      url: access.url,
      type: 'audio/mpeg',
      supportsByteRanges: true,
    });
    expect(createObjectUrl).not.toHaveBeenCalled();
  });

  it('refreshes a 401 once and retries the capability call with the fresh bearer', async () => {
    setAccessToken('expired');
    const fetchSpy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(problem(401, 'Unauthorized'))
      .mockResolvedValueOnce(json(access));
    registerRefreshHandler(async () => setAccessToken('fresh'));

    await createDocumentAccessUrl(document.id, 'preview');
    const retryHeaders = new Headers((fetchSpy.mock.calls[1]?.[1] as RequestInit).headers);
    expect(retryHeaders.get('Authorization')).toBe('Bearer fresh');
  });

  it('reads a transcript page around a media citation timestamp', async () => {
    const page = {
      document_id: document.id,
      duration_ms: 60_000,
      language: 'en',
      transcription_model: 'x-ai/grok-stt-1.0',
      speakers: [],
      items: [],
      next_cursor: null,
    };
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(json(page));
    expect(await fetchDocumentTranscript(document.id, { around_ms: 12_500, limit: 50 })).toEqual(
      page,
    );
    const url = String(fetchSpy.mock.calls[0]?.[0]);
    expect(url).toContain('around_ms=12500');
    expect(url).toContain('limit=50');
  });

  it('preserves opaque visibility failures', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(problem(404, 'Not Found'));
    await expect(createDocumentAccessUrl('hidden', 'preview')).rejects.toMatchObject({
      status: 404,
    });
  });
});
