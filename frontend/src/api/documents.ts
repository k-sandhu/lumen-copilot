/**
 * Typed collections + documents calls — part of the api/ boundary (#49). The
 * ONLY place the SPA performs HTTP for the documents slice (ADR-0004); features
 * consume hooks built on these, never `fetch`/`XMLHttpRequest` directly.
 *
 * Conforms to the FROZEN contract (contracts/openapi.yaml 0.1.0):
 *   GET    /collections                 → CollectionList
 *   POST   /collections                 → Collection (201)
 *   PATCH  /collections/{id}            → Collection
 *   DELETE /collections/{id}            → 204
 *   GET    /documents?collection_id&status&q&cursor&limit → DocumentList
 *   POST   /documents (multipart)        → Document (201) | 413 | 415 | 422
 *   GET    /documents/{id}               → Document
 *   DELETE /documents/{id}               → 204
 *   GET    /documents/{id}/content       → 200 bytes | 302 presigned URL
 *
 * Negative paths honored (spec 0004): cross-tenant / not-permitted reads return
 * 404 (INV-1/INV-2), never 403; the client surfaces the typed Problem body so
 * the UI can branch on shape, not status text.
 */
import { request, ApiError } from './client';
import { API_BASE_URL } from './env';
import { getAccessToken } from './token';
import type {
  Collection,
  CollectionCreate,
  CollectionList,
  CollectionUpdate,
  Document,
  DocumentList,
  DocumentListQuery,
  Problem,
} from './types';

/** Cursor-page params shared by the list endpoints. */
export interface PageQuery {
  cursor?: string;
  limit?: number;
}

function buildQuery(params: Record<string, string | number | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== '') search.set(key, String(value));
  }
  const qs = search.toString();
  return qs ? `?${qs}` : '';
}

// --- Collections ----------------------------------------------------------

/** List the caller's collections (one cursor page). */
export function listCollections(page: PageQuery = {}, signal?: AbortSignal): Promise<CollectionList> {
  return request<CollectionList>(`/collections${buildQuery({ ...page })}`, { signal });
}

/** Create a collection. */
export function createCollection(body: CollectionCreate): Promise<Collection> {
  return request<Collection>('/collections', { method: 'POST', json: body });
}

/** Rename / re-describe a collection (PATCH — at least one field). */
export function updateCollection(id: string, body: CollectionUpdate): Promise<Collection> {
  return request<Collection>(`/collections/${id}`, { method: 'PATCH', json: body });
}

/** Delete a collection and its documents (204). */
export function deleteCollection(id: string): Promise<void> {
  return request<void>(`/collections/${id}`, { method: 'DELETE' });
}

// --- Documents (read / delete) --------------------------------------------

/** List documents, optionally filtered by collection / status / filename. */
export function listDocuments(query: DocumentListQuery = {}, signal?: AbortSignal): Promise<DocumentList> {
  const qs = buildQuery({
    collection_id: query.collection_id,
    status: query.status,
    q: query.q,
    cursor: query.cursor,
    limit: query.limit,
  });
  return request<DocumentList>(`/documents${qs}`, { signal });
}

/** Get one document's metadata (incl. ingestion status). */
export function getDocument(id: string, signal?: AbortSignal): Promise<Document> {
  return request<Document>(`/documents/${id}`, { signal });
}

/** Delete a document and its chunks/embeddings (204). */
export function deleteDocument(id: string): Promise<void> {
  return request<void>(`/documents/${id}`, { method: 'DELETE' });
}

// --- Upload (multipart + progress) ----------------------------------------

export interface UploadDocumentArgs {
  file: File;
  collectionId: string;
  /** Progress 0..1, fired as the file body uploads. */
  onProgress?: (fraction: number) => void;
  /** Abort the in-flight upload (e.g. user cancels / navigates away). */
  signal?: AbortSignal;
}

function joinApi(path: string): string {
  const b = API_BASE_URL.endsWith('/') ? API_BASE_URL.slice(0, -1) : API_BASE_URL;
  const p = path.startsWith('/') ? path : `/${path}`;
  return `${b}${p}`;
}

function problemFrom(text: string): Problem | undefined {
  try {
    const data: unknown = JSON.parse(text);
    if (
      typeof data === 'object' &&
      data !== null &&
      typeof (data as Record<string, unknown>).title === 'string' &&
      typeof (data as Record<string, unknown>).status === 'number'
    ) {
      return data as Problem;
    }
  } catch {
    /* non-JSON body */
  }
  return undefined;
}

/**
 * Upload a file into a collection via `POST /documents` (multipart/form-data).
 *
 * Uses XMLHttpRequest, NOT fetch — fetch exposes no upload-progress event, and
 * AC-2 requires a real progress bar. The token is bearered onto the request and
 * the refresh cookie rides along via `withCredentials`. A 413/415/422/404 maps
 * to a typed `ApiError` carrying the Problem body so the UI shows a clear inline
 * error (AC-4 / INV-8). A network failure is ApiError status 0.
 */
export function uploadDocument({
  file,
  collectionId,
  onProgress,
  signal,
}: UploadDocumentArgs): Promise<Document> {
  return new Promise<Document>((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    const url = joinApi('/documents');
    xhr.open('POST', url);
    xhr.withCredentials = true;

    const token = getAccessToken();
    if (token) xhr.setRequestHeader('Authorization', `Bearer ${token}`);
    xhr.setRequestHeader('Accept', 'application/json, application/problem+json');

    onProgress?.(0);
    xhr.upload.onprogress = (event: ProgressEvent): void => {
      if (event.lengthComputable && event.total > 0) {
        onProgress?.(event.loaded / event.total);
      }
    };

    xhr.onload = (): void => {
      if (xhr.status >= 200 && xhr.status < 300) {
        try {
          resolve(JSON.parse(xhr.responseText) as Document);
        } catch {
          reject(new ApiError('Upload succeeded but the response was unreadable', xhr.status));
        }
        return;
      }
      reject(
        new ApiError(
          `Upload to ${url} failed with ${xhr.status}`,
          xhr.status,
          problemFrom(xhr.responseText),
        ),
      );
    };
    xhr.onerror = (): void => reject(new ApiError('Upload network request failed', 0));
    xhr.onabort = (): void => reject(new ApiError('Upload aborted', 0));

    if (signal) {
      if (signal.aborted) {
        reject(new ApiError('Upload aborted', 0));
        return;
      }
      signal.addEventListener('abort', () => xhr.abort(), { once: true });
    }

    const form = new FormData();
    // Order: file then collection_id (contract requires both; order is irrelevant
    // to the server but file-first keeps the multipart boundary scan small).
    form.append('file', file);
    form.append('collection_id', collectionId);
    xhr.send(form);
  });
}

// --- Document content (viewer): follow a 302 → presigned URL ---------------

/**
 * Resolve a viewable URL for a document's original bytes (AC-3).
 *
 * `GET /documents/{id}/content` may either stream the bytes (200) or 302 to a
 * short-TTL presigned URL (CC-12). We issue a `redirect: 'manual'` request so we
 * can READ the `Location` header rather than have the browser transparently
 * follow it (a cross-origin presigned URL would otherwise be opaque). On 200 we
 * use the endpoint URL itself as the viewer `src` (the bearer rides via the
 * normal client on a subsequent fetch by the <img>/<iframe> — same-origin proxy).
 *
 * A 404 (cross-tenant / not permitted, INV-2) surfaces as a typed ApiError.
 */
export async function resolveDocumentContentUrl(id: string, signal?: AbortSignal): Promise<string> {
  const url = joinApi(`/documents/${id}/content`);
  const token = getAccessToken();
  const headers = new Headers({ Accept: 'application/octet-stream, application/problem+json' });
  if (token) headers.set('Authorization', `Bearer ${token}`);

  let response: Response;
  try {
    response = await fetch(url, {
      method: 'GET',
      credentials: 'include',
      redirect: 'manual',
      headers,
      signal,
    });
  } catch (cause) {
    throw new ApiError(cause instanceof Error ? cause.message : 'Network request failed', 0);
  }

  // `redirect: 'manual'` surfaces a redirect as an opaqueredirect response
  // (status 0, type 'opaqueredirect') OR, in non-browser/test fetches, as a real
  // 3xx with a readable Location. Handle both.
  if (response.status === 302 || response.status === 301 || response.status === 307) {
    const location = response.headers.get('Location');
    if (location) return location;
  }

  if (response.status >= 200 && response.status < 400) {
    // Bytes are streamed same-origin; the endpoint URL is itself the viewer src.
    return url;
  }

  let problem: Problem | undefined;
  try {
    const data: unknown = await response.json();
    if (
      typeof data === 'object' &&
      data !== null &&
      typeof (data as Record<string, unknown>).title === 'string'
    ) {
      problem = data as Problem;
    }
  } catch {
    /* non-JSON body */
  }
  throw new ApiError(`Content request for ${id} failed with ${response.status}`, response.status, problem);
}

// --- Document content (chat citation viewer, #50) --------------------------

/**
 * Same-origin URL for a document's original bytes (`GET /documents/{id}/content`).
 * The server may 200 the bytes or 302 to a short-TTL presigned URL; either way
 * this is the URL the viewer points an <iframe>/<a> at. It rides the httpOnly
 * session cookie (credentials are same-origin); a not-permitted id yields 404.
 */
export function documentContentUrl(documentId: string): string {
  const base = API_BASE_URL.endsWith('/') ? API_BASE_URL.slice(0, -1) : API_BASE_URL;
  return `${base}/documents/${documentId}/content`;
}
