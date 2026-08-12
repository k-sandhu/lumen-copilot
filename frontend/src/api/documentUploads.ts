/**
 * Direct multipart document/media uploads (spec 0008 / #571).
 *
 * FastAPI is only the authenticated control plane. File slices are PUT straight
 * to the signed object-storage URLs without the Lumen bearer token or cookies.
 * The manager bounds work to three files and four parts per file, resumes from
 * provider-verified completed parts, retries transient/expired URLs with
 * exponential jitter, and aborts the provider upload when the caller cancels.
 */
import { ApiError, requestV2 } from './client';
import type {
  CompleteUploadPart,
  Document,
  DocumentUploadCreate,
  DocumentUploadSession,
  SignedUploadPart,
  SignedUploadPartList,
} from './types';

export interface UploadedPartEtag {
  part_number: number;
  etag: string;
}

export function initiateDocumentUpload(
  metadata: DocumentUploadCreate,
  signal?: AbortSignal,
): Promise<DocumentUploadSession> {
  return requestV2<DocumentUploadSession>('/document-uploads', {
    method: 'POST',
    json: metadata,
    signal,
  });
}

export function getDocumentUpload(
  uploadId: string,
  signal?: AbortSignal,
): Promise<DocumentUploadSession> {
  return requestV2<DocumentUploadSession>(`/document-uploads/${uploadId}`, { signal });
}

export function signDocumentUploadParts(
  uploadId: string,
  partNumbers: number[],
  signal?: AbortSignal,
): Promise<SignedUploadPartList> {
  return requestV2<SignedUploadPartList>(`/document-uploads/${uploadId}/parts`, {
    method: 'POST',
    json: { part_numbers: partNumbers },
    signal,
  });
}

export function completeDocumentUpload(
  uploadId: string,
  parts: CompleteUploadPart[],
  signal?: AbortSignal,
): Promise<Document> {
  return requestV2<Document>(`/document-uploads/${uploadId}/complete`, {
    method: 'POST',
    json: { parts },
    signal,
  });
}

export function abortDocumentUpload(uploadId: string): Promise<void> {
  return requestV2<void>(`/document-uploads/${uploadId}`, { method: 'DELETE' });
}

/** A storage-plane failure. It deliberately carries no provider response body. */
export class StorageUploadError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = 'StorageUploadError';
    this.status = status;
  }
}

/**
 * PUT one File.slice() directly to object storage with progress.
 *
 * Only the exact server-signed headers are applied. `withCredentials=false`
 * and no Authorization header are non-negotiable data-plane invariants.
 */
export function putSignedUploadPart(
  part: SignedUploadPart,
  body: Blob,
  onProgress?: (loadedBytes: number) => void,
  signal?: AbortSignal,
): Promise<UploadedPartEtag> {
  return new Promise<UploadedPartEtag>((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    let settled = false;
    const abortXhr = (): void => {
      xhr.abort();
      fail(abortError());
    };
    const cleanup = (): void => {
      xhr.upload.onprogress = null;
      xhr.onload = null;
      xhr.onerror = null;
      xhr.onabort = null;
      signal?.removeEventListener('abort', abortXhr);
    };
    const succeed = (value: UploadedPartEtag): void => {
      if (settled) return;
      settled = true;
      cleanup();
      resolve(value);
    };
    function fail(error: unknown): void {
      if (settled) return;
      settled = true;
      cleanup();
      reject(error);
    }

    xhr.open('PUT', part.url);
    xhr.withCredentials = false;
    for (const [name, value] of Object.entries(part.required_headers)) {
      xhr.setRequestHeader(name, value);
    }

    onProgress?.(0);
    xhr.upload.onprogress = (event: ProgressEvent): void => {
      onProgress?.(Math.min(body.size, event.loaded));
    };
    xhr.onload = (): void => {
      if (xhr.status >= 200 && xhr.status < 300) {
        const etag = xhr.getResponseHeader('ETag');
        if (!etag) {
          fail(new StorageUploadError('Storage did not expose the uploaded part ETag', 502));
          return;
        }
        onProgress?.(body.size);
        succeed({ part_number: part.part_number, etag });
        return;
      }
      fail(new StorageUploadError(`Storage part upload failed with ${xhr.status}`, xhr.status));
    };
    xhr.onerror = (): void => fail(new StorageUploadError('Storage upload network failed', 0));
    xhr.onabort = (): void => fail(abortError());

    if (signal) {
      if (signal.aborted) {
        fail(abortError());
        return;
      }
      signal.addEventListener('abort', abortXhr, { once: true });
    }
    xhr.send(body);
  });
}

export type DirectUploadPhase = 'queued' | 'initiating' | 'resuming' | 'uploading' | 'completing';

export interface DirectUploadProgress {
  phase: DirectUploadPhase;
  loadedBytes: number;
  totalBytes: number;
  /** Safe aggregate fraction in [0, 1]. */
  fraction: number;
}

export interface DirectUploadInput {
  file: File;
  collectionId: string;
  /** Continue an in-memory retry using the authoritative server part list. */
  resumeUploadId?: string;
  signal?: AbortSignal;
  onProgress?: (progress: DirectUploadProgress) => void;
  /** Called as soon as the durable server upload id exists. */
  onSession?: (session: DocumentUploadSession) => void;
}

export interface DirectUploadTransport {
  initiate: (
    metadata: DocumentUploadCreate,
    signal?: AbortSignal,
  ) => Promise<DocumentUploadSession>;
  get: (uploadId: string, signal?: AbortSignal) => Promise<DocumentUploadSession>;
  signParts: (
    uploadId: string,
    partNumbers: number[],
    signal?: AbortSignal,
  ) => Promise<SignedUploadPartList>;
  putPart: (
    part: SignedUploadPart,
    body: Blob,
    onProgress?: (loadedBytes: number) => void,
    signal?: AbortSignal,
  ) => Promise<UploadedPartEtag>;
  complete: (
    uploadId: string,
    parts: CompleteUploadPart[],
    signal?: AbortSignal,
  ) => Promise<Document>;
  abort: (uploadId: string) => Promise<void>;
}

export interface DirectUploadManagerOptions {
  maxConcurrentFiles?: number;
  maxConcurrentParts?: number;
  /** Number of retries after the initial storage attempt. */
  maxRetries?: number;
  retryBaseMs?: number;
  sleep?: (milliseconds: number) => Promise<void>;
  random?: () => number;
}

/** Error enriched with the resumable server session id when one was created. */
export class DirectUploadError extends Error {
  readonly causeValue: unknown;
  readonly uploadId?: string;
  readonly cancelled: boolean;

  constructor(
    message: string,
    causeValue: unknown,
    options: { uploadId?: string; cancelled?: boolean } = {},
  ) {
    super(message);
    this.name = 'DirectUploadError';
    this.causeValue = causeValue;
    this.uploadId = options.uploadId;
    this.cancelled = options.cancelled ?? false;
  }
}

const DEFAULT_TRANSPORT: DirectUploadTransport = {
  initiate: initiateDocumentUpload,
  get: getDocumentUpload,
  signParts: signDocumentUploadParts,
  putPart: putSignedUploadPart,
  complete: completeDocumentUpload,
  abort: abortDocumentUpload,
};

/** Frozen OpenAPI UploadPartNumberList.maxItems and backend signing batch size. */
const MAX_SIGNED_PARTS_PER_REQUEST = 100;

export class DirectUploadManager {
  private readonly maxConcurrentFiles: number;
  private readonly maxConcurrentParts: number;
  private readonly maxRetries: number;
  private readonly retryBaseMs: number;
  private readonly sleep: (milliseconds: number) => Promise<void>;
  private readonly random: () => number;
  private activeFiles = 0;
  private readonly waitingFiles: Array<() => void> = [];

  constructor(
    private readonly transport: DirectUploadTransport = DEFAULT_TRANSPORT,
    options: DirectUploadManagerOptions = {},
  ) {
    this.maxConcurrentFiles = options.maxConcurrentFiles ?? 3;
    this.maxConcurrentParts = options.maxConcurrentParts ?? 4;
    this.maxRetries = options.maxRetries ?? 3;
    this.retryBaseMs = options.retryBaseMs ?? 400;
    this.sleep = options.sleep ?? ((ms) => new Promise((resolve) => setTimeout(resolve, ms)));
    this.random = options.random ?? Math.random;
  }

  async upload(input: DirectUploadInput): Promise<Document> {
    input.onProgress?.({
      phase: 'queued',
      loadedBytes: 0,
      totalBytes: input.file.size,
      fraction: 0,
    });
    let acquired = false;
    try {
      await this.acquireFile(input.signal);
      acquired = true;
      return await this.run(input);
    } catch (error) {
      if (error instanceof DirectUploadError) throw error;
      const cancelled = input.signal?.aborted === true || isAbort(error);
      throw new DirectUploadError(cancelled ? 'Upload cancelled' : errorMessage(error), error, {
        cancelled,
      });
    } finally {
      if (acquired) this.releaseFile();
    }
  }

  private async run(input: DirectUploadInput): Promise<Document> {
    const controller = new AbortController();
    const forwardAbort = (): void => controller.abort();
    input.signal?.addEventListener('abort', forwardAbort, { once: true });
    if (input.signal?.aborted) controller.abort();
    let current: DocumentUploadSession | undefined;
    let completionParts: CompleteUploadPart[] | undefined;

    try {
      this.emit(input, input.resumeUploadId ? 'resuming' : 'initiating', 0);
      current = input.resumeUploadId
        ? await this.getResumableSession(input.resumeUploadId, controller.signal)
        : await this.transport.initiate(
            fileMetadata(input.file, input.collectionId),
            controller.signal,
          );
      input.onSession?.(current);
      assertResumableFile(current, input.file, input.collectionId);

      if (current.state === 'completed' && current.document) return current.document;
      if (current.state !== 'initiated') {
        throw new ApiError(current.error ?? `Upload is ${current.state}`, 409);
      }

      const completed = new Map(
        current.completed_parts.map((part) => [part.part_number, part.etag] as const),
      );
      const completedBytes = current.completed_parts.reduce(
        (sum, part) => sum + part.size_bytes,
        0,
      );
      const inflightBytes = new Map<number, number>();
      const emitUploadProgress = (): void => {
        const loaded = Math.min(
          input.file.size,
          completedBytes + [...inflightBytes.values()].reduce((sum, value) => sum + value, 0),
        );
        this.emit(input, 'uploading', loaded);
      };
      emitUploadProgress();

      const pendingNumbers = Array.from(
        { length: current.part_count },
        (_, index) => index + 1,
      ).filter((partNumber) => !completed.has(partNumber));

      const partController = new AbortController();
      const abortParts = (): void => partController.abort();
      controller.signal.addEventListener('abort', abortParts, { once: true });
      if (controller.signal.aborted) partController.abort();
      try {
        // Keep capabilities short-lived in practice as well as in contract: sign
        // at most one server-sized page, drain it, then mint the next page.
        for (
          let offset = 0;
          offset < pendingNumbers.length;
          offset += MAX_SIGNED_PARTS_PER_REQUEST
        ) {
          const page = pendingNumbers.slice(offset, offset + MAX_SIGNED_PARTS_PER_REQUEST);
          const response = await this.transport.signParts(current.id, page, partController.signal);
          const signedByNumber = new Map(
            response.items.map((item) => [item.part_number, item] as const),
          );
          await mapBounded(
            page,
            this.maxConcurrentParts,
            async (partNumber) => {
              const start = (partNumber - 1) * current!.part_size_bytes;
              const end = Math.min(input.file.size, start + current!.part_size_bytes);
              const body = input.file.slice(start, end, input.file.type || current!.mime_type);
              let signed = requireSigned(signedByNumber, partNumber);
              let result: UploadedPartEtag | undefined;

              for (let attempt = 0; attempt <= this.maxRetries; attempt += 1) {
                try {
                  result = await this.transport.putPart(
                    signed,
                    body,
                    (loaded) => {
                      inflightBytes.set(partNumber, loaded);
                      emitUploadProgress();
                    },
                    partController.signal,
                  );
                  break;
                } catch (error) {
                  if (
                    partController.signal.aborted ||
                    !isRetriableStorageError(error) ||
                    attempt >= this.maxRetries
                  ) {
                    throw error;
                  }
                  const ceiling = this.retryBaseMs * 2 ** attempt;
                  await abortableSleep(
                    this.sleep,
                    Math.floor(this.random() * ceiling),
                    partController.signal,
                  );
                  const refreshed = await this.transport.signParts(
                    current!.id,
                    [partNumber],
                    partController.signal,
                  );
                  signed = requireSigned(
                    new Map(refreshed.items.map((item) => [item.part_number, item])),
                    partNumber,
                  );
                }
              }

              if (!result) throw new StorageUploadError('Storage upload did not complete', 0);
              inflightBytes.set(partNumber, body.size);
              completed.set(partNumber, result.etag);
              emitUploadProgress();
            },
            () => partController.abort(),
          );
        }
      } finally {
        controller.signal.removeEventListener('abort', abortParts);
      }

      completionParts = [...completed.entries()]
        .sort(([left], [right]) => left - right)
        .map(([part_number, etag]) => ({ part_number, etag }));
      this.emit(input, 'completing', input.file.size);
      const document = await this.transport.complete(
        current.id,
        completionParts,
        controller.signal,
      );
      this.emit(input, 'completing', input.file.size);
      return document;
    } catch (error) {
      const cancelled = controller.signal.aborted || isAbort(error);
      if (cancelled && current?.state === 'initiated' && completionParts) {
        try {
          // Completion is an idempotent commit boundary. If the response was
          // aborted after storage/database commit, confirm it without the
          // cancelled signal instead of aborting a possibly completed session.
          const document = await this.transport.complete(current.id, completionParts);
          this.emit(input, 'completing', input.file.size);
          return document;
        } catch (reconcileError) {
          throw new DirectUploadError(
            'Upload finalization could not be confirmed. Retry to reconcile it.',
            reconcileError,
            { uploadId: current.id },
          );
        }
      }
      if (cancelled && current?.state === 'initiated') {
        try {
          await this.transport.abort(current.id);
        } catch {
          // The janitor still fails closed; preserve the user's original cancel.
        }
      }
      if (error instanceof DirectUploadError) throw error;
      const uploadId =
        !cancelled && current?.state === 'initiated'
          ? current.id
          : !current && input.resumeUploadId && isRetriableControlError(error)
            ? input.resumeUploadId
            : undefined;
      throw new DirectUploadError(cancelled ? 'Upload cancelled' : errorMessage(error), error, {
        ...(uploadId ? { uploadId } : {}),
        cancelled,
      });
    } finally {
      input.signal?.removeEventListener('abort', forwardAbort);
    }
  }

  private emit(input: DirectUploadInput, phase: DirectUploadPhase, loadedBytes: number): void {
    const totalBytes = input.file.size;
    input.onProgress?.({
      phase,
      loadedBytes,
      totalBytes,
      fraction: totalBytes > 0 ? Math.max(0, Math.min(1, loadedBytes / totalBytes)) : 0,
    });
  }

  private async getResumableSession(
    uploadId: string,
    signal: AbortSignal,
  ): Promise<DocumentUploadSession> {
    for (let attempt = 0; ; attempt += 1) {
      try {
        return await this.transport.get(uploadId, signal);
      } catch (error) {
        if (signal.aborted || !isRetriableControlError(error) || attempt >= this.maxRetries) {
          throw error;
        }
        const ceiling = this.retryBaseMs * 2 ** attempt;
        await abortableSleep(this.sleep, Math.floor(this.random() * ceiling), signal);
      }
    }
  }

  private acquireFile(signal?: AbortSignal): Promise<void> {
    if (signal?.aborted) return Promise.reject(abortError());
    if (this.activeFiles < this.maxConcurrentFiles) {
      this.activeFiles += 1;
      return Promise.resolve();
    }
    return new Promise<void>((resolve, reject) => {
      const start = (): void => {
        signal?.removeEventListener('abort', cancel);
        this.activeFiles += 1;
        resolve();
      };
      const cancel = (): void => {
        const index = this.waitingFiles.indexOf(start);
        if (index >= 0) this.waitingFiles.splice(index, 1);
        reject(abortError());
      };
      signal?.addEventListener('abort', cancel, { once: true });
      this.waitingFiles.push(start);
    });
  }

  private releaseFile(): void {
    this.activeFiles = Math.max(0, this.activeFiles - 1);
    this.waitingFiles.shift()?.();
  }
}

/** Shared process-local queue: no more than three files transfer at once. */
export const documentUploadManager = new DirectUploadManager();

function fileMetadata(file: File, collectionId: string): DocumentUploadCreate {
  const modified = file.lastModified > 0 ? new Date(file.lastModified) : null;
  return {
    filename: file.name,
    mime_type: declaredMime(file),
    size_bytes: file.size,
    collection_id: collectionId,
    ...(modified && !Number.isNaN(modified.valueOf())
      ? { last_modified_at: modified.toISOString() }
      : {}),
  };
}

function assertResumableFile(
  current: DocumentUploadSession,
  file: File,
  collectionId: string,
): void {
  if (
    current.filename !== file.name ||
    current.size_bytes !== file.size ||
    current.collection_id !== collectionId ||
    current.mime_type !== declaredMime(file)
  ) {
    throw new ApiError('The selected file does not match this upload session', 422);
  }
}

const ACCEPTED_EXTENSION_MIME: Readonly<Record<string, string>> = {
  pdf: 'application/pdf',
  docx: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  pptx: 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
  xlsx: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  txt: 'text/plain',
  md: 'text/markdown',
  wav: 'audio/wav',
  mp3: 'audio/mpeg',
  m4a: 'audio/mp4',
  aac: 'audio/aac',
  flac: 'audio/flac',
  ogg: 'audio/ogg',
  mp4: 'video/mp4',
  webm: 'video/webm',
};

const ACCEPTED_MIME_ALIASES: Readonly<Record<string, ReadonlySet<string>>> = {
  m4a: new Set(['audio/x-m4a', 'audio/m4a']),
  mp3: new Set(['audio/mp3']),
  wav: new Set(['audio/vnd.wave', 'audio/x-wav', 'audio/wave']),
};

/** Canonicalize only a declared generic/known alias backed by an accepted suffix. */
function declaredMime(file: File): string {
  const browserType = file.type.split(';', 1)[0]?.trim().toLowerCase();
  const extension = file.name.split('.').pop()?.toLowerCase();
  const inferred = extension ? ACCEPTED_EXTENSION_MIME[extension] : undefined;
  if (!browserType) {
    if (inferred) return inferred;
    throw new ApiError('This file type isn’t supported.', 415);
  }
  if (browserType === 'application/octet-stream') {
    if (inferred) return inferred;
    throw new ApiError('This file type isn’t supported.', 415);
  }
  if (inferred && extension !== undefined && ACCEPTED_MIME_ALIASES[extension]?.has(browserType)) {
    return inferred;
  }
  return browserType;
}

function requireSigned(
  signed: Map<number, SignedUploadPart>,
  partNumber: number,
): SignedUploadPart {
  const part = signed.get(partNumber);
  if (!part) throw new ApiError(`No signed URL was returned for part ${partNumber}`, 502);
  return part;
}

function isRetriableStorageError(error: unknown): boolean {
  if (!(error instanceof StorageUploadError)) return false;
  return (
    error.status === 0 ||
    error.status === 401 ||
    error.status === 403 ||
    error.status === 408 ||
    error.status === 425 ||
    error.status === 429 ||
    error.status >= 500
  );
}

function isRetriableControlError(error: unknown): boolean {
  if (!(error instanceof ApiError)) return false;
  return (
    error.status === 0 ||
    error.status === 408 ||
    error.status === 425 ||
    error.status === 429 ||
    error.status >= 500
  );
}

function abortError(): DOMException {
  return new DOMException('The operation was aborted', 'AbortError');
}

function isAbort(error: unknown): boolean {
  return error instanceof DOMException && error.name === 'AbortError';
}

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.displayMessage;
  if (error instanceof Error) return error.message;
  return 'Upload failed';
}

async function mapBounded<T>(
  items: T[],
  concurrency: number,
  run: (item: T) => Promise<void>,
  onFailure?: (error: unknown) => void,
): Promise<void> {
  let cursor = 0;
  let failed = false;
  let firstError: unknown;
  const workers = Array.from({ length: Math.min(concurrency, items.length) }, async () => {
    while (!failed && cursor < items.length) {
      const index = cursor;
      cursor += 1;
      try {
        await run(items[index]!);
      } catch (error) {
        if (!failed) {
          failed = true;
          firstError = error;
          onFailure?.(error);
        }
      }
    }
  });
  await Promise.all(workers);
  if (failed) throw firstError;
}

/** Race retry delay against cancellation without retaining abort listeners. */
function abortableSleep(
  sleep: (milliseconds: number) => Promise<void>,
  milliseconds: number,
  signal: AbortSignal,
): Promise<void> {
  if (signal.aborted) return Promise.reject(abortError());
  return new Promise<void>((resolve, reject) => {
    let settled = false;
    const finish = (error?: unknown): void => {
      if (settled) return;
      settled = true;
      signal.removeEventListener('abort', cancel);
      if (error === undefined) resolve();
      else reject(error);
    };
    const cancel = (): void => finish(abortError());
    signal.addEventListener('abort', cancel, { once: true });
    void sleep(milliseconds).then(
      () => finish(),
      (error: unknown) => finish(error),
    );
  });
}
