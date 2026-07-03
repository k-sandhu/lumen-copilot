/**
 * Typed collections + documents calls — part of the api/ boundary (#49). The
 * ONLY place the SPA performs HTTP for the documents slice (ADR-0004); features
 * consume hooks built on these, never `fetch`/`XMLHttpRequest` directly.
 *
 * Also hosts the chat citation click-through path (#50): the viewer loads a
 * permitted document's bytes so it can open at the cited passage (spec 0004
 * INV-2/INV-3 — the backend returns 404 for a document the caller may not see).
 *
 * Conforms to the FROZEN contract (contracts/openapi.yaml 0.6.0):
 *   GET    /collections                 → CollectionList
 *   POST   /collections                 → Collection (201)
 *   PATCH  /collections/{id}            → Collection
 *   DELETE /collections/{id}            → 204
 *   GET    /documents?collection_id&status&q&cursor&limit → DocumentList
 *   POST   /documents (multipart)        → Document (201) | 413 | 415 | 422
 *   GET    /documents/{id}               → Document
 *   DELETE /documents/{id}               → 204
 *   GET    /documents/{id}/content       → 200 bytes | 302 presigned URL
 *   GET    /documents/{id}/text          → DocumentText | 404 | 409
 *
 * Negative paths honored (spec 0004): cross-tenant / not-permitted reads return
 * 404 (INV-1/INV-2), never 403; the client surfaces the typed Problem body so
 * the UI can branch on shape, not status text.
 */
import { request, ApiError, withRefreshRetry } from './client';
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
  DocumentText,
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

function joinApi(path: string): string {
  const b = API_BASE_URL.endsWith('/') ? API_BASE_URL.slice(0, -1) : API_BASE_URL;
  const p = path.startsWith('/') ? path : `/${path}`;
  return `${b}${p}`;
}

/** Coerce a parsed body into a Problem when it has the required shape. */
function problemFrom(value: unknown): Problem | undefined {
  if (typeof value !== 'object' || value === null) return undefined;
  const v = value as Record<string, unknown>;
  if (typeof v.title === 'string' && typeof v.status === 'number') return value as Problem;
  return undefined;
}

/** Parse a (possibly non-JSON) response body string into a Problem, if it is one. */
function problemFromText(text: string): Problem | undefined {
  try {
    return problemFrom(JSON.parse(text));
  } catch {
    return undefined;
  }
}

// --- Collections ----------------------------------------------------------

/** List the caller's collections (one cursor page). */
export function listCollections(
  page: PageQuery = {},
  signal?: AbortSignal,
): Promise<CollectionList> {
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
export function listDocuments(
  query: DocumentListQuery = {},
  signal?: AbortSignal,
): Promise<DocumentList> {
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
          problemFromText(xhr.responseText),
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

// --- Document content (both viewers, #242) ----------------------------------
//
// There is exactly ONE way to load a document's bytes: an authenticated fetch
// that follows the contract's optional 302→presigned redirect and hands back a
// `blob:` object URL. The older `resolveDocumentContentUrl` (read the Location
// from a `redirect:'manual'` response) was removed (#242): in a real browser a
// manual cross-origin redirect is an *opaqueredirect* — status 0, headers
// unreadable — so it failed for every document while passing in test fetches.

/** A loadable document content source, plus a revoke for the object URL we minted. */
export interface DocumentContent {
  /** `blob:` URL to point the viewer's <iframe>/<a> at. */
  url: string;
  /**
   * The blob's content type (the stored upload content-type, carried through
   * the presigned response). Lets a viewer branch on type when the caller
   * doesn't know it (chat citations carry no mime type on the wire).
   */
  type: string;
  /** Release the underlying object URL. */
  revoke: () => void;
}

/**
 * Load a permitted document's content for the citation viewer (AC-2).
 *
 * INV-4 / contract: `GET /documents/{id}/content` is a `bearerAuth` endpoint —
 * it is authorized by the in-memory access JWT, NOT by a cookie. (The only
 * cookie is the httpOnly REFRESH cookie, used solely by /auth/refresh +
 * /auth/logout.) A browser-initiated `<iframe src>` / `<a href>` GET cannot
 * attach an `Authorization: Bearer` header, so it would 401. We therefore fetch
 * the bytes here with the bearer attached and hand back a `blob:` object URL the
 * viewer can safely render.
 *
 * The endpoint may instead 302 to a short-TTL presigned URL (CC-12). We issue a
 * `redirect: 'follow'` fetch so the browser transparently follows that redirect
 * and streams the bytes; the resulting body is still turned into a blob URL, so
 * the viewer never has to re-request (and never leaks the access token into an
 * iframe/anchor URL). A 404 (cross-tenant / not permitted, INV-2) and a 401
 * surface as a typed ApiError carrying the Problem body.
 */
export async function fetchDocumentContent(
  documentId: string,
  signal?: AbortSignal,
): Promise<DocumentContent> {
  const url = joinApi(`/documents/${documentId}/content`);
  return withRefreshRetry(async () => {
    const headers = new Headers({ Accept: 'application/octet-stream, application/problem+json' });
    const token = getAccessToken();
    if (token) headers.set('Authorization', `Bearer ${token}`);

    let response: Response;
    try {
      // credentials:'include' lets the refresh cookie ride along, but the actual
      // authorization is the bearer header above; redirect:'follow' transparently
      // chases the contract's optional 302 → presigned URL.
      response = await fetch(url, {
        method: 'GET',
        credentials: 'include',
        redirect: 'follow',
        headers,
        signal,
      });
    } catch (cause) {
      throw new ApiError(cause instanceof Error ? cause.message : 'Network request failed', 0);
    }

    if (!response.ok) {
      let problem: Problem | undefined;
      try {
        problem = problemFrom(await response.json());
      } catch {
        /* non-JSON body */
      }
      throw new ApiError(
        `Content request for ${documentId} failed with ${response.status}`,
        response.status,
        problem,
      );
    }

    const blob = await response.blob();
    const objectUrl = URL.createObjectURL(blob);
    return { url: objectUrl, type: blob.type, revoke: () => URL.revokeObjectURL(objectUrl) };
  });
}

// --- Extracted text (contract 0.6.0, #245) ----------------------------------

/**
 * Fetch the extracted plain text of a ready document (`GET /documents/{id}/text`).
 *
 * The viewer's text surface for formats a browser cannot render natively
 * (DOCX/PPTX/XLSX). 404 = not visible (INV-2); 409 = not ready yet
 * (`document_not_ready`); both surface as typed ApiErrors via the client.
 */
export function fetchDocumentText(id: string, signal?: AbortSignal): Promise<DocumentText> {
  return request<DocumentText>(`/documents/${id}/text`, { signal });
}
