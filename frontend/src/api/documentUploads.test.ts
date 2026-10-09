import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from './client';
import {
  DirectUploadManager,
  StorageUploadError,
  type UploadedPartEtag,
  putSignedUploadPart,
  initiateDocumentUpload,
  type DirectUploadTransport,
} from './documentUploads';
import type { Document, DocumentUploadSession, SignedUploadPart, UploadedPart } from './types';

const COLLECTION_ID = '11111111-1111-4111-8111-111111111111';

afterEach(() => vi.restoreAllMocks());

function session(overrides: Partial<DocumentUploadSession> = {}): DocumentUploadSession {
  return {
    id: '22222222-2222-4222-8222-222222222222',
    document_id: '33333333-3333-4333-8333-333333333333',
    state: 'initiated',
    filename: 'meeting.mp3',
    mime_type: 'audio/mpeg',
    size_bytes: 10,
    collection_id: COLLECTION_ID,
    part_size_bytes: 2,
    part_count: 5,
    completed_parts: [],
    expires_at: '2030-01-01T00:00:00Z',
    document: null,
    created_at: '2026-08-11T00:00:00Z',
    updated_at: '2026-08-11T00:00:00Z',
    ...overrides,
  };
}

function document(): Document {
  return {
    id: '33333333-3333-4333-8333-333333333333',
    filename: 'meeting.mp3',
    mime_type: 'audio/mpeg',
    size_bytes: 10,
    collection_id: COLLECTION_ID,
    owner_id: '44444444-4444-4444-8444-444444444444',
    kind: 'audio',
    duration_ms: null,
    status: 'pending',
    chunk_count: 0,
    created_at: '2026-08-11T00:00:00Z',
    updated_at: '2026-08-11T00:00:00Z',
  };
}

function signed(partNumber: number): SignedUploadPart {
  return {
    part_number: partNumber,
    url: `https://storage.example/upload/${partNumber}`,
    expires_at: '2030-01-01T00:00:00Z',
    required_headers: { 'Content-Type': 'audio/mpeg' },
  };
}

function transport(overrides: Partial<DirectUploadTransport> = {}): DirectUploadTransport {
  const current = session();
  return {
    initiate: vi.fn(async () => current),
    get: vi.fn(async () => current),
    signParts: vi.fn(async (_uploadId, partNumbers) => ({
      items: partNumbers.map(signed),
    })),
    putPart: vi.fn(async (part, body, onProgress) => {
      onProgress(body.size);
      return { part_number: part.part_number, etag: `etag-${part.part_number}` };
    }),
    complete: vi.fn(async () => document()),
    abort: vi.fn(async () => undefined),
    ...overrides,
  };
}

describe('DirectUploadManager', () => {
  it('infers an allowlisted MIME when the browser leaves File.type empty', async () => {
    const t = transport({
      initiate: vi.fn(async (metadata) =>
        session({ filename: metadata.filename, mime_type: metadata.mime_type }),
      ),
    });
    const manager = new DirectUploadManager(t);
    await manager.upload({
      file: new File(['abcdefghij'], 'field-recording.flac'),
      collectionId: COLLECTION_ID,
    });

    expect(t.initiate).toHaveBeenCalledWith(
      expect.objectContaining({ mime_type: 'audio/flac' }),
      expect.any(AbortSignal),
    );
  });

  it('canonicalizes browser MIME case and parameters before initiation and comparison', async () => {
    const t = transport({
      initiate: vi.fn(async (metadata) =>
        session({
          filename: metadata.filename,
          mime_type: metadata.mime_type,
          part_size_bytes: 10,
          part_count: 1,
        }),
      ),
    });
    const manager = new DirectUploadManager(t);

    await manager.upload({
      file: new File(['abcdefghij'], 'meeting.mp4', {
        type: 'Video/MP4; codecs=avc1',
      }),
      collectionId: COLLECTION_ID,
    });

    expect(t.initiate).toHaveBeenCalledWith(
      expect.objectContaining({ mime_type: 'video/mp4' }),
      expect.any(AbortSignal),
    );
  });

  it.each([
    ['recording.m4a', 'audio/x-m4a', 'audio/mp4'],
    ['interview.mp3', 'audio/mp3', 'audio/mpeg'],
    ['voice-note.wav', 'audio/vnd.wave', 'audio/wav'],
    ['archive.flac', 'application/octet-stream', 'audio/flac'],
  ])('canonicalizes the accepted %s browser alias %s', async (filename, browserType, expected) => {
    const t = transport({
      initiate: vi.fn(async (metadata) =>
        session({
          filename: metadata.filename,
          mime_type: metadata.mime_type,
          part_size_bytes: 10,
          part_count: 1,
        }),
      ),
    });
    const manager = new DirectUploadManager(t);

    await manager.upload({
      file: new File(['abcdefghij'], filename, { type: browserType }),
      collectionId: COLLECTION_ID,
    });

    expect(t.initiate).toHaveBeenCalledWith(
      expect.objectContaining({ mime_type: expected }),
      expect.any(AbortSignal),
    );
  });

  it.each(['', 'application/octet-stream'])(
    'rejects an unknown extension with browser type %j before initiation',
    async (browserType) => {
      const t = transport();
      const manager = new DirectUploadManager(t);

      await expect(
        manager.upload({
          file: new File(['abcdefghij'], 'mystery.bin', { type: browserType }),
          collectionId: COLLECTION_ID,
        }),
      ).rejects.toMatchObject({ causeValue: expect.objectContaining({ status: 415 }) });
      expect(t.initiate).not.toHaveBeenCalled();
    },
  );

  it('runs no more than three whole-file uploads at once', async () => {
    let initiated = 0;
    const completions: Array<(value: Document) => void> = [];
    const t = transport({
      initiate: vi.fn(async (metadata) => {
        initiated += 1;
        return session({
          id: `22222222-2222-4222-8222-${String(initiated).padStart(12, '0')}`,
          document_id: `33333333-3333-4333-8333-${String(initiated).padStart(12, '0')}`,
          filename: metadata.filename,
          part_size_bytes: 10,
          part_count: 1,
        });
      }),
      complete: vi.fn(
        () =>
          new Promise<Document>((resolve) => {
            completions.push(resolve);
          }),
      ),
    });
    const manager = new DirectUploadManager(t, { maxConcurrentFiles: 3 });
    const uploads = Array.from({ length: 5 }, (_, index) =>
      manager.upload({
        file: new File(['abcdefghij'], `meeting-${index}.mp3`, { type: 'audio/mpeg' }),
        collectionId: COLLECTION_ID,
      }),
    );

    await vi.waitFor(() => expect(t.initiate).toHaveBeenCalledTimes(3));
    expect(completions).toHaveLength(3);
    completions.splice(0).forEach((finish) => finish(document()));
    await vi.waitFor(() => expect(t.initiate).toHaveBeenCalledTimes(5));
    completions.splice(0).forEach((finish) => finish(document()));
    await expect(Promise.all(uploads)).resolves.toHaveLength(5);
  });

  it('reports cancellation while waiting for a file slot as cancelled', async () => {
    let finishFirst: ((value: Document) => void) | undefined;
    const t = transport({
      initiate: vi.fn(async (metadata) =>
        session({
          filename: metadata.filename,
          part_size_bytes: 10,
          part_count: 1,
        }),
      ),
      complete: vi.fn(
        () =>
          new Promise<Document>((resolve) => {
            finishFirst = resolve;
          }),
      ),
    });
    const manager = new DirectUploadManager(t, { maxConcurrentFiles: 1 });
    const first = manager.upload({
      file: new File(['abcdefghij'], 'first.mp3', { type: 'audio/mpeg' }),
      collectionId: COLLECTION_ID,
    });
    await vi.waitFor(() => expect(t.complete).toHaveBeenCalledTimes(1));

    const controller = new AbortController();
    const queued = manager.upload({
      file: new File(['abcdefghij'], 'queued.mp3', { type: 'audio/mpeg' }),
      collectionId: COLLECTION_ID,
      signal: controller.signal,
    });
    controller.abort();

    await expect(queued).rejects.toMatchObject({ cancelled: true, uploadId: undefined });
    expect(t.initiate).toHaveBeenCalledTimes(1);
    expect(t.abort).not.toHaveBeenCalled();
    finishFirst?.(document());
    await expect(first).resolves.toMatchObject({ id: document().id });
  });

  it('uploads no more than four parts for one file at once', async () => {
    let active = 0;
    let peak = 0;
    const gates: Array<() => void> = [];
    const t = transport({
      putPart: vi.fn(async (part, _body, onProgress) => {
        active += 1;
        peak = Math.max(peak, active);
        await new Promise<void>((resolve) => gates.push(resolve));
        active -= 1;
        onProgress(2);
        return { part_number: part.part_number, etag: `etag-${part.part_number}` };
      }),
    });
    const manager = new DirectUploadManager(t, { maxConcurrentFiles: 3, maxConcurrentParts: 4 });
    const upload = manager.upload({
      file: new File(['abcdefghij'], 'meeting.mp3', { type: 'audio/mpeg' }),
      collectionId: COLLECTION_ID,
    });

    await vi.waitFor(() => expect(gates).toHaveLength(4));
    expect(peak).toBe(4);
    gates.splice(0).forEach((release) => release());
    await vi.waitFor(() => expect(gates).toHaveLength(1));
    gates.splice(0).forEach((release) => release());
    await expect(upload).resolves.toMatchObject({ id: document().id });
  });

  it('signs and drains at most one 100-part capability page at a time', async () => {
    let releaseFirstPart: (() => void) | undefined;
    const firstPartGate = new Promise<void>((resolve) => {
      releaseFirstPart = resolve;
    });
    const t = transport({
      initiate: vi.fn(async (metadata) =>
        session({
          filename: metadata.filename,
          size_bytes: metadata.size_bytes,
          part_size_bytes: 2,
          part_count: 101,
        }),
      ),
      putPart: vi.fn(async (part, body, onProgress) => {
        if (part.part_number === 1) await firstPartGate;
        onProgress?.(body.size);
        return { part_number: part.part_number, etag: `etag-${part.part_number}` };
      }),
    });
    const manager = new DirectUploadManager(t, { maxConcurrentParts: 4 });
    const upload = manager.upload({
      file: new File(['x'.repeat(202)], 'long-meeting.mp3', { type: 'audio/mpeg' }),
      collectionId: COLLECTION_ID,
    });

    await vi.waitFor(() => expect(t.putPart).toHaveBeenCalledTimes(100));
    expect(t.signParts).toHaveBeenCalledTimes(1);
    expect(t.signParts).toHaveBeenNthCalledWith(
      1,
      session().id,
      Array.from({ length: 100 }, (_, index) => index + 1),
      expect.any(AbortSignal),
    );

    releaseFirstPart?.();
    await expect(upload).resolves.toMatchObject({ id: document().id });
    expect(t.signParts).toHaveBeenCalledTimes(2);
    expect(t.signParts).toHaveBeenNthCalledWith(2, session().id, [101], expect.any(AbortSignal));
  });

  it('aborts and drains sibling parts before releasing a failed file slot', async () => {
    const firstUploadId = 'upload-first';
    let initiation = 0;
    let firstPartsStarted = 0;
    let failTerminalPart: (() => void) | undefined;
    let releaseAbortedSibling: (() => void) | undefined;
    let siblingSawAbort = false;
    const t = transport({
      initiate: vi.fn(async (metadata) => {
        initiation += 1;
        return session({
          id: initiation === 1 ? firstUploadId : 'upload-second',
          document_id: initiation === 1 ? 'document-first' : 'document-second',
          filename: metadata.filename,
          part_size_bytes: 5,
          part_count: 2,
        });
      }),
      signParts: vi.fn(async (uploadId: string, partNumbers: number[]) => ({
        items: partNumbers.map((partNumber) => ({
          ...signed(partNumber),
          url: `https://storage.example/${uploadId}/${partNumber}`,
        })),
      })),
      putPart: vi.fn((part, body, onProgress, signal) => {
        if (!part.url.includes(firstUploadId)) {
          onProgress?.(body.size);
          return Promise.resolve({
            part_number: part.part_number,
            etag: `etag-${part.part_number}`,
          });
        }
        firstPartsStarted += 1;
        if (part.part_number === 1) {
          return new Promise<UploadedPartEtag>((_resolve, reject) => {
            failTerminalPart = () =>
              reject(new StorageUploadError('terminal provider rejection', 400));
          });
        }
        return new Promise<UploadedPartEtag>((_resolve, reject) => {
          signal?.addEventListener(
            'abort',
            () => {
              siblingSawAbort = true;
              releaseAbortedSibling = () =>
                reject(new DOMException('sibling aborted', 'AbortError'));
            },
            { once: true },
          );
        });
      }),
    });
    const manager = new DirectUploadManager(t, {
      maxConcurrentFiles: 1,
      maxConcurrentParts: 2,
      maxRetries: 0,
    });
    const first = manager
      .upload({
        file: new File(['abcdefghij'], 'first.mp3', { type: 'audio/mpeg' }),
        collectionId: COLLECTION_ID,
      })
      .catch((error: unknown) => error);
    const second = manager.upload({
      file: new File(['abcdefghij'], 'second.mp3', { type: 'audio/mpeg' }),
      collectionId: COLLECTION_ID,
    });

    await vi.waitFor(() => expect(firstPartsStarted).toBe(2));
    failTerminalPart?.();
    await vi.waitFor(() => expect(siblingSawAbort).toBe(true));
    expect(t.initiate).toHaveBeenCalledTimes(1);

    releaseAbortedSibling?.();
    await expect(first).resolves.toMatchObject({
      cancelled: false,
      uploadId: firstUploadId,
    });
    await expect(second).resolves.toMatchObject({ id: document().id });
    expect(t.initiate).toHaveBeenCalledTimes(2);
    expect(t.abort).not.toHaveBeenCalled();
  });

  it('resumes from provider-verified completed parts without re-uploading them', async () => {
    const completed: UploadedPart = { part_number: 1, etag: 'existing-etag', size_bytes: 2 };
    const resumed = session({ completed_parts: [completed] });
    const t = transport({ get: vi.fn(async () => resumed) });
    const manager = new DirectUploadManager(t);

    await manager.upload({
      file: new File(['abcdefghij'], 'meeting.mp3', { type: 'audio/mpeg' }),
      collectionId: COLLECTION_ID,
      resumeUploadId: resumed.id,
    });

    expect(t.initiate).not.toHaveBeenCalled();
    expect(t.signParts).toHaveBeenCalledWith(resumed.id, [2, 3, 4, 5], expect.any(AbortSignal));
    expect(t.putPart).toHaveBeenCalledTimes(4);
    expect(t.complete).toHaveBeenCalledWith(
      resumed.id,
      [
        { part_number: 1, etag: 'existing-etag' },
        { part_number: 2, etag: 'etag-2' },
        { part_number: 3, etag: 'etag-3' },
        { part_number: 4, etag: 'etag-4' },
        { part_number: 5, etag: 'etag-5' },
      ],
      expect.any(AbortSignal),
    );
  });

  it('accepts backend recovery of a durable completing session as completed', async () => {
    const recovered = session({ state: 'completed', document: document() });
    const t = transport({ get: vi.fn(async () => recovered) });
    const manager = new DirectUploadManager(t);

    await expect(
      manager.upload({
        file: new File(['abcdefghij'], 'meeting.mp3', { type: 'audio/mpeg' }),
        collectionId: COLLECTION_ID,
        resumeUploadId: recovered.id,
      }),
    ).resolves.toMatchObject({ id: document().id });

    expect(t.initiate).not.toHaveBeenCalled();
    expect(t.signParts).not.toHaveBeenCalled();
    expect(t.complete).not.toHaveBeenCalled();
  });

  it('retries a transient GET while the backend reconciles a completing session', async () => {
    const recovered = session({ state: 'completed', document: document() });
    const get = vi
      .fn<DirectUploadTransport['get']>()
      .mockRejectedValueOnce(new ApiError('Recovery is temporarily unavailable', 503))
      .mockResolvedValueOnce(recovered);
    const sleep = vi.fn(async () => undefined);
    const t = transport({ get });
    const manager = new DirectUploadManager(t, { maxRetries: 1, sleep, random: () => 0 });

    await expect(
      manager.upload({
        file: new File(['abcdefghij'], 'meeting.mp3', { type: 'audio/mpeg' }),
        collectionId: COLLECTION_ID,
        resumeUploadId: recovered.id,
      }),
    ).resolves.toMatchObject({ id: document().id });

    expect(get).toHaveBeenCalledTimes(2);
    expect(sleep).toHaveBeenCalledTimes(1);
  });

  it.each(['expired', 'failed', 'aborted'] as const)(
    'clears the resumable id when GET reports a terminal %s session',
    async (state) => {
      const terminal = session({ state, error: `Upload is ${state}` });
      const t = transport({ get: vi.fn(async () => terminal) });
      const manager = new DirectUploadManager(t);

      await expect(
        manager.upload({
          file: new File(['abcdefghij'], 'meeting.mp3', { type: 'audio/mpeg' }),
          collectionId: COLLECTION_ID,
          resumeUploadId: terminal.id,
        }),
      ).rejects.toMatchObject({ cancelled: false, uploadId: undefined });
    },
  );

  it('re-signs and retries a transient or expired storage URL', async () => {
    const t = transport();
    vi.mocked(t.putPart)
      .mockRejectedValueOnce(new StorageUploadError('expired', 403))
      .mockImplementation(async (part) => ({
        part_number: part.part_number,
        etag: `etag-${part.part_number}`,
      }));
    const sleep = vi.fn(async () => undefined);
    const manager = new DirectUploadManager(t, { maxRetries: 2, sleep, random: () => 0 });

    await manager.upload({
      file: new File(['abcdefghij'], 'meeting.mp3', { type: 'audio/mpeg' }),
      collectionId: COLLECTION_ID,
    });

    expect(t.putPart).toHaveBeenCalledTimes(6);
    expect(t.signParts).toHaveBeenCalledTimes(2);
    expect(sleep).toHaveBeenCalledTimes(1);
  });

  it('aborts active storage work and the provider session when cancelled', async () => {
    let started: (() => void) | undefined;
    const t = transport({
      putPart: vi.fn(
        (_part, _body, _onProgress, signal) =>
          new Promise<UploadedPartEtag>((_resolve, reject) => {
            started?.();
            signal?.addEventListener('abort', () =>
              reject(new DOMException('aborted', 'AbortError')),
            );
          }),
      ),
    });
    const manager = new DirectUploadManager(t);
    const controller = new AbortController();
    const partStarted = new Promise<void>((resolve) => {
      started = resolve;
    });
    const upload = manager.upload({
      file: new File(['abcdefghij'], 'meeting.mp3', { type: 'audio/mpeg' }),
      collectionId: COLLECTION_ID,
      signal: controller.signal,
    });
    await partStarted;
    controller.abort();

    await expect(upload).rejects.toMatchObject({ cancelled: true, uploadId: undefined });
    expect(t.abort).toHaveBeenCalledWith(session().id);
    expect(t.complete).not.toHaveBeenCalled();
  });

  it('reconciles completion when the server committed before its response was aborted', async () => {
    const controller = new AbortController();
    let completeCalls = 0;
    const t = transport({
      initiate: vi.fn(async () => session({ part_size_bytes: 10, part_count: 1 })),
      complete: vi.fn((_uploadId, _parts, signal) => {
        completeCalls += 1;
        if (completeCalls > 1) return Promise.resolve(document());
        return new Promise<Document>((_resolve, reject) => {
          signal?.addEventListener(
            'abort',
            () => reject(new DOMException('response aborted', 'AbortError')),
            { once: true },
          );
          queueMicrotask(() => controller.abort());
        });
      }),
    });
    const manager = new DirectUploadManager(t);

    await expect(
      manager.upload({
        file: new File(['abcdefghij'], 'meeting.mp3', { type: 'audio/mpeg' }),
        collectionId: COLLECTION_ID,
        signal: controller.signal,
      }),
    ).resolves.toMatchObject({ id: document().id });

    expect(t.complete).toHaveBeenCalledTimes(2);
    expect(vi.mocked(t.complete).mock.calls[1]?.[2]).toBeUndefined();
    expect(t.abort).not.toHaveBeenCalled();
  });
});

describe('v2 upload control-plane routing', () => {
  it('calls /api/v2/document-uploads rather than nesting v2 under the v1 base', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify(session()), {
        status: 201,
        headers: { 'Content-Type': 'application/json' },
      }),
    );
    await initiateDocumentUpload({
      filename: 'meeting.mp3',
      mime_type: 'audio/mpeg',
      size_bytes: 10,
      collection_id: COLLECTION_ID,
    });

    const url = String(fetchSpy.mock.calls[0]?.[0]);
    expect(url).toContain('/api/v2/document-uploads');
    expect(url).not.toContain('/api/v1/v2/');
  });
});

describe('putSignedUploadPart', () => {
  it('sends only signed storage headers and never bearer credentials or cookies', async () => {
    class FakeXhr {
      static latest: FakeXhr | null = null;
      readonly upload: { onprogress: ((event: ProgressEvent) => void) | null } = {
        onprogress: null,
      };
      readonly headers = new Map<string, string>();
      withCredentials = true;
      status = 200;
      onload: (() => void) | null = null;
      onerror: (() => void) | null = null;
      onabort: (() => void) | null = null;
      method = '';
      url = '';

      constructor() {
        FakeXhr.latest = this;
      }

      open(method: string, url: string) {
        this.method = method;
        this.url = url;
      }

      setRequestHeader(name: string, value: string) {
        this.headers.set(name, value);
      }

      getResponseHeader(name: string) {
        return name.toLowerCase() === 'etag' ? '"opaque-etag"' : null;
      }

      send() {
        this.onload?.();
      }

      abort() {
        this.onabort?.();
      }
    }

    const original = globalThis.XMLHttpRequest;
    globalThis.XMLHttpRequest = FakeXhr as unknown as typeof XMLHttpRequest;
    try {
      const signal = new AbortController().signal;
      const removeSignalListener = vi.spyOn(signal, 'removeEventListener');
      await expect(
        putSignedUploadPart(signed(1), new Blob(['ab']), undefined, signal),
      ).resolves.toEqual({ part_number: 1, etag: '"opaque-etag"' });
      expect(FakeXhr.latest?.method).toBe('PUT');
      expect(FakeXhr.latest?.withCredentials).toBe(false);
      expect([...(FakeXhr.latest?.headers ?? new Map()).keys()]).toEqual(['Content-Type']);
      expect(FakeXhr.latest?.headers.has('Authorization')).toBe(false);
      expect(removeSignalListener).toHaveBeenCalledWith('abort', expect.any(Function));
    } finally {
      globalThis.XMLHttpRequest = original;
    }
  });
});
