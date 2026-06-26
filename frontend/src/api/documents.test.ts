/**
 * Typed collections + documents API calls — part of the api/ boundary (#49),
 * built against the FROZEN contract (contracts/openapi.yaml 0.1.0).
 *
 * Covers the happy paths AND the negative categories the contract names:
 *   - cross-tenant / not-permitted resource → 404 (INV-1 / INV-2)
 *   - over-size upload → 413, unsupported type → 415, malformed body → 422 (INV-8)
 * Upload uses XHR (fetch has no upload-progress event); the content endpoint
 * follows a 302 to a presigned URL.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import {
  listCollections,
  createCollection,
  updateCollection,
  deleteCollection,
  listDocuments,
  getDocument,
  uploadDocument,
  deleteDocument,
  resolveDocumentContentUrl,
  fetchDocumentContent,
  ApiError,
  registerRefreshHandler,
} from './index';
import { setAccessToken, clearAccessToken } from './token';

function jsonResponse(body: unknown, status = 200, contentType = 'application/json'): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': contentType } });
}

function problem(status: number, title: string, detail?: string, code?: string): Response {
  return new Response(JSON.stringify({ type: 'about:blank', title, status, detail, code }), {
    status,
    headers: { 'Content-Type': 'application/problem+json' },
  });
}

const sampleCollection = {
  id: '11111111-1111-1111-1111-111111111111',
  name: 'Acme contracts',
  owner_id: '22222222-2222-2222-2222-222222222222',
  document_count: 2,
  created_at: '2026-06-18T00:00:00Z',
  updated_at: '2026-06-18T00:00:00Z',
};

const sampleDocument = {
  id: '33333333-3333-3333-3333-333333333333',
  filename: 'msa.pdf',
  mime_type: 'application/pdf',
  size_bytes: 1024,
  collection_id: sampleCollection.id,
  owner_id: sampleCollection.owner_id,
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
  it('lists collections (GET /collections)', async () => {
    const spy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(jsonResponse({ items: [sampleCollection], next_cursor: null }));
    const page = await listCollections();
    expect(page.items).toHaveLength(1);
    expect(page.items[0]?.name).toBe('Acme contracts');
    const url = spy.mock.calls[0]?.[0] as string;
    expect(url).toContain('/collections');
  });

  it('passes cursor + limit through as query params', async () => {
    const spy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(jsonResponse({ items: [], next_cursor: null }));
    await listCollections({ cursor: 'abc', limit: 50 });
    const url = spy.mock.calls[0]?.[0] as string;
    expect(url).toContain('cursor=abc');
    expect(url).toContain('limit=50');
  });

  it('creates a collection (POST /collections)', async () => {
    const spy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(jsonResponse(sampleCollection, 201));
    const created = await createCollection({ name: 'Acme contracts' });
    expect(created.id).toBe(sampleCollection.id);
    const init = spy.mock.calls[0]?.[1] as RequestInit;
    expect(init.method).toBe('POST');
    expect(JSON.parse(init.body as string)).toEqual({ name: 'Acme contracts' });
  });

  it('surfaces a 422 on a malformed create body (INV-8)', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      problem(422, 'Unprocessable Entity', 'name is required'),
    );
    await expect(createCollection({ name: '' })).rejects.toMatchObject({ status: 422 });
  });

  it('renames a collection (PATCH /collections/{id})', async () => {
    const spy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(jsonResponse({ ...sampleCollection, name: 'Renamed' }));
    const updated = await updateCollection(sampleCollection.id, { name: 'Renamed' });
    expect(updated.name).toBe('Renamed');
    const init = spy.mock.calls[0]?.[1] as RequestInit;
    expect(init.method).toBe('PATCH');
  });

  it('deletes a collection (DELETE → 204)', async () => {
    const spy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(new Response(null, { status: 204 }));
    await expect(deleteCollection(sampleCollection.id)).resolves.toBeUndefined();
    const init = spy.mock.calls[0]?.[1] as RequestInit;
    expect(init.method).toBe('DELETE');
  });

  it('maps a cross-tenant / missing collection to 404 (INV-1)', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(problem(404, 'Not Found'));
    await expect(deleteCollection('00000000-0000-0000-0000-000000000000')).rejects.toMatchObject({
      status: 404,
    });
  });
});

describe('documents api', () => {
  it('lists documents with collection / status / q filters', async () => {
    const spy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(jsonResponse({ items: [sampleDocument], next_cursor: null }));
    await listDocuments({ collection_id: sampleCollection.id, status: 'ready', q: 'msa' });
    const url = spy.mock.calls[0]?.[0] as string;
    expect(url).toContain(`collection_id=${sampleCollection.id}`);
    expect(url).toContain('status=ready');
    expect(url).toContain('q=msa');
  });

  it('omits absent filters from the query string', async () => {
    const spy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(jsonResponse({ items: [], next_cursor: null }));
    await listDocuments({ collection_id: sampleCollection.id });
    const url = spy.mock.calls[0]?.[0] as string;
    expect(url).not.toContain('status=');
    expect(url).not.toContain('q=');
  });

  it('gets one document (GET /documents/{id})', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse(sampleDocument));
    const doc = await getDocument(sampleDocument.id);
    expect(doc.status).toBe('ready');
  });

  it('maps a direct fetch of a not-permitted document to 404 (INV-2)', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(problem(404, 'Not Found'));
    await expect(getDocument('00000000-0000-0000-0000-000000000000')).rejects.toBeInstanceOf(
      ApiError,
    );
  });

  it('deletes a document (DELETE → 204)', async () => {
    const spy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(new Response(null, { status: 204 }));
    await deleteDocument(sampleDocument.id);
    const init = spy.mock.calls[0]?.[1] as RequestInit;
    expect(init.method).toBe('DELETE');
  });
});

// XHR-backed upload — fetch exposes no upload-progress event, so the client
// uses XMLHttpRequest. We stub a minimal XHR so the test stays transport-pure.
interface XhrStub {
  open: ReturnType<typeof vi.fn>;
  send: ReturnType<typeof vi.fn>;
  setRequestHeader: ReturnType<typeof vi.fn>;
  upload: { onprogress: ((e: ProgressEvent) => void) | null };
  onload: (() => void) | null;
  onerror: (() => void) | null;
  onabort: (() => void) | null;
  status: number;
  responseText: string;
  readonly DONE: number;
}

function installXhr(): { last: XhrStub } {
  const ref: { last: XhrStub } = { last: undefined as unknown as XhrStub };
  class FakeXhr {
    open = vi.fn();
    setRequestHeader = vi.fn();
    upload: XhrStub['upload'] = { onprogress: null };
    onload: (() => void) | null = null;
    onerror: (() => void) | null = null;
    onabort: (() => void) | null = null;
    status = 0;
    responseText = '';
    readonly DONE = 4;
    send = vi.fn();
    constructor() {
      ref.last = this as unknown as XhrStub;
    }
  }
  vi.stubGlobal('XMLHttpRequest', FakeXhr as unknown as typeof XMLHttpRequest);
  return ref;
}

describe('uploadDocument (multipart + progress, XHR)', () => {
  afterEach(() => vi.unstubAllGlobals());

  function file(name = 'msa.pdf', type = 'application/pdf', size = 10): File {
    return new File([new Uint8Array(size)], name, { type });
  }

  it('POSTs multipart form-data, bearers the token, and resolves the Document', async () => {
    setAccessToken('jwt-upload');
    const ref = installXhr();
    const promise = uploadDocument({ file: file(), collectionId: sampleCollection.id });

    const xhr = ref.last;
    expect(xhr.open).toHaveBeenCalledWith('POST', expect.stringContaining('/documents'));
    // bearer header set (INV-4 wiring)
    expect(xhr.setRequestHeader).toHaveBeenCalledWith('Authorization', 'Bearer jwt-upload');
    // multipart body is a FormData with the file + collection_id
    const body = xhr.send.mock.calls[0]?.[0] as FormData;
    expect(body).toBeInstanceOf(FormData);
    expect(body.get('collection_id')).toBe(sampleCollection.id);
    expect(body.get('file')).toBeInstanceOf(File);

    xhr.status = 201;
    xhr.responseText = JSON.stringify(sampleDocument);
    xhr.onload?.();

    const doc = await promise;
    expect(doc.id).toBe(sampleDocument.id);
  });

  it('reports progress via the onProgress callback', async () => {
    const ref = installXhr();
    const seen: number[] = [];
    const promise = uploadDocument({
      file: file(),
      collectionId: sampleCollection.id,
      onProgress: (p) => seen.push(p),
    });
    const xhr = ref.last;
    xhr.upload.onprogress?.({ lengthComputable: true, loaded: 50, total: 100 } as ProgressEvent);
    xhr.upload.onprogress?.({ lengthComputable: true, loaded: 100, total: 100 } as ProgressEvent);
    xhr.status = 201;
    xhr.responseText = JSON.stringify(sampleDocument);
    xhr.onload?.();
    await promise;
    // Starts at 0 immediately (bar appears at 0%), then tracks the upload body.
    expect(seen).toEqual([0, 0.5, 1]);
  });

  it('rejects an over-size upload as ApiError 413 with the Problem body (AC-4)', async () => {
    const ref = installXhr();
    const promise = uploadDocument({ file: file(), collectionId: sampleCollection.id });
    const xhr = ref.last;
    xhr.status = 413;
    xhr.responseText = JSON.stringify({
      type: 'about:blank',
      title: 'Payload Too Large',
      status: 413,
      detail: 'Max upload size is 25 MB.',
    });
    xhr.onload?.();
    await expect(promise).rejects.toMatchObject({ status: 413 });
    await promise.catch((e: ApiError) => {
      expect(e).toBeInstanceOf(ApiError);
      expect(e.problem?.title).toBe('Payload Too Large');
    });
  });

  it('rejects an unsupported type as ApiError 415 (AC-4)', async () => {
    const ref = installXhr();
    const promise = uploadDocument({
      file: file('virus.exe', 'application/x-msdownload'),
      collectionId: sampleCollection.id,
    });
    const xhr = ref.last;
    xhr.status = 415;
    xhr.responseText = JSON.stringify({
      type: 'about:blank',
      title: 'Unsupported Media Type',
      status: 415,
    });
    xhr.onload?.();
    await expect(promise).rejects.toMatchObject({ status: 415 });
  });

  it('rejects a missing/cross-tenant collection as ApiError 404 (INV-1)', async () => {
    const ref = installXhr();
    const promise = uploadDocument({ file: file(), collectionId: 'nope' });
    const xhr = ref.last;
    xhr.status = 404;
    xhr.responseText = JSON.stringify({ type: 'about:blank', title: 'Not Found', status: 404 });
    xhr.onload?.();
    await expect(promise).rejects.toMatchObject({ status: 404 });
  });

  it('wraps a network failure as ApiError status 0', async () => {
    const ref = installXhr();
    const promise = uploadDocument({ file: file(), collectionId: sampleCollection.id });
    ref.last.onerror?.();
    await expect(promise).rejects.toMatchObject({ status: 0 });
  });
});

describe('resolveDocumentContentUrl (follow 302 → presigned URL)', () => {
  afterEach(() => vi.restoreAllMocks());

  it('returns the same-origin content URL when the server streams bytes (200)', async () => {
    // A plain manual-redirect fetch that comes back 200 (no redirect) → use the
    // endpoint URL directly as the viewer src.
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(null, { status: 200 }));
    const url = await resolveDocumentContentUrl(sampleDocument.id);
    expect(url).toContain(`/documents/${sampleDocument.id}/content`);
  });

  it('follows a 302 and returns the presigned Location URL (AC-3)', async () => {
    const presigned = 'https://minio.example/bucket/obj?sig=abc';
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(null, { status: 302, headers: { Location: presigned } }),
    );
    const url = await resolveDocumentContentUrl(sampleDocument.id);
    expect(url).toBe(presigned);
  });

  it('throws ApiError on a 404 (not permitted / cross-tenant — INV-2)', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(problem(404, 'Not Found'));
    await expect(resolveDocumentContentUrl('nope')).rejects.toMatchObject({ status: 404 });
  });

  it('refreshes once, retries, and returns the presigned URL after an expired-token 401', async () => {
    setAccessToken('expired');
    const presigned = 'https://minio.example/bucket/obj?sig=fresh';
    const fetchSpy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(problem(401, 'Unauthorized'))
      .mockResolvedValueOnce(new Response(null, { status: 302, headers: { Location: presigned } }));
    const handler = vi.fn(async () => {
      setAccessToken('fresh');
    });
    registerRefreshHandler(handler);

    const url = await resolveDocumentContentUrl(sampleDocument.id);

    expect(url).toBe(presigned);
    expect(handler).toHaveBeenCalledTimes(1);
    expect(fetchSpy).toHaveBeenCalledTimes(2);
    const retryInit = fetchSpy.mock.calls[1]?.[1] as RequestInit;
    const retryHeaders = new Headers(retryInit.headers);
    expect(retryInit.redirect).toBe('manual');
    expect(retryHeaders.get('Authorization')).toBe('Bearer fresh');
  });

  it('propagates the original 401 when refresh fails', async () => {
    setAccessToken('expired');
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(problem(401, 'Unauthorized'));
    const handler = vi.fn(async () => {
      clearAccessToken();
      throw new Error('refresh failed');
    });
    registerRefreshHandler(handler);

    await expect(resolveDocumentContentUrl(sampleDocument.id)).rejects.toMatchObject({
      status: 401,
    });
    expect(handler).toHaveBeenCalledTimes(1);
  });

  it('only retries once when the content endpoint keeps returning 401', async () => {
    setAccessToken('expired');
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(problem(401, 'Unauthorized'));
    const handler = vi.fn(async () => {
      setAccessToken('still-bad');
    });
    registerRefreshHandler(handler);

    await expect(resolveDocumentContentUrl(sampleDocument.id)).rejects.toMatchObject({
      status: 401,
    });
    expect(fetchSpy).toHaveBeenCalledTimes(2);
    expect(handler).toHaveBeenCalledTimes(1);
  });
});

function installObjectUrl(): {
  create: ReturnType<typeof vi.fn>;
  revoke: ReturnType<typeof vi.fn>;
} {
  const create = vi.fn(() => 'blob:doc-content');
  const revoke = vi.fn();
  Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: create });
  Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: revoke });
  return { create, revoke };
}

describe('fetchDocumentContent (bearer fetch → blob URL)', () => {
  it('refreshes once, retries with the fresh bearer, and returns a blob URL', async () => {
    setAccessToken('expired');
    const urls = installObjectUrl();
    const fetchSpy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(problem(401, 'Unauthorized'))
      .mockResolvedValueOnce(
        new Response(new Blob(['pdf-bytes'], { type: 'application/pdf' }), { status: 200 }),
      );
    const handler = vi.fn(async () => {
      setAccessToken('fresh');
    });
    registerRefreshHandler(handler);

    const content = await fetchDocumentContent(sampleDocument.id);

    expect(content.url).toBe('blob:doc-content');
    content.revoke();
    expect(urls.create).toHaveBeenCalledTimes(1);
    expect(urls.revoke).toHaveBeenCalledWith('blob:doc-content');
    expect(handler).toHaveBeenCalledTimes(1);
    expect(fetchSpy).toHaveBeenCalledTimes(2);
    const retryInit = fetchSpy.mock.calls[1]?.[1] as RequestInit;
    const retryHeaders = new Headers(retryInit.headers);
    expect(retryInit.redirect).toBe('follow');
    expect(retryHeaders.get('Authorization')).toBe('Bearer fresh');
  });

  it('still surfaces a non-auth content failure as an ApiError', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(problem(404, 'Not Found'));

    await expect(fetchDocumentContent('nope')).rejects.toMatchObject({ status: 404 });
  });
});
