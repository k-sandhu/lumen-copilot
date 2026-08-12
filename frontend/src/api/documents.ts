/**
 * Typed collections + documents calls — part of the api/ boundary (#49). The
 * ONLY place the SPA performs HTTP for the documents slice (ADR-0004); features
 * consume hooks built on these, never `fetch`/`XMLHttpRequest` directly.
 *
 * Also hosts signed preview/download capabilities and paginated transcripts;
 * file bytes flow browser ↔ object storage, never through FastAPI (#571).
 *
 * Conforms to the FROZEN contract (contracts/openapi.yaml 0.6.0):
 *   GET    /collections                 → CollectionList
 *   POST   /collections                 → Collection (201)
 *   PATCH  /collections/{id}            → Collection
 *   DELETE /collections/{id}            → 204
 *   GET    /documents?collection_id&status&q&cursor&limit → DocumentList
 *   POST   /api/v2/document-uploads      → metadata-only multipart session
 *   GET    /documents/{id}               → Document
 *   DELETE /documents/{id}               → 204
 *   POST   /api/v2/documents/{id}/access-url → signed JSON capability
 *   GET    /api/v2/documents/{id}/transcript → timestamped transcript page
 *   GET    /documents/{id}/text          → DocumentText | 404 | 409
 *
 * Negative paths honored (spec 0004): cross-tenant / not-permitted reads return
 * 404 (INV-1/INV-2), never 403; the client surfaces the typed Problem body so
 * the UI can branch on shape, not status text.
 */
import { request, requestV2 } from './client';
import { documentUploadManager } from './documentUploads';
import type {
  Collection,
  CollectionCreate,
  CollectionList,
  CollectionUpdate,
  Document,
  DocumentList,
  DocumentListQuery,
  DocumentText,
  DocumentAccessPurpose,
  DocumentAccessUrl,
  TranscriptPage,
  TranscriptQuery,
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

/** Compatibility wrapper over the v2 direct multipart manager. */
export function uploadDocument({
  file,
  collectionId,
  onProgress,
  signal,
}: UploadDocumentArgs): Promise<Document> {
  return documentUploadManager.upload({
    file,
    collectionId,
    signal,
    onProgress: ({ fraction }) => onProgress?.(fraction),
  });
}

// --- Signed object access + transcript (spec 0008 / #571) -----------------

/** A loadable document content source, plus a revoke for the object URL we minted. */
export interface DocumentContent {
  /** Short-lived signed storage URL; Range requests never cross FastAPI. */
  url: string;
  /**
   * Validated stored content type returned by the authenticated control plane.
   */
  type: string;
  expiresAt: string;
  filename: string;
  sizeBytes: number;
  supportsByteRanges: boolean;
  /** Kept for old consumers; signed URLs do not allocate browser object URLs. */
  revoke: () => void;
}

/** Mint a preview capability while preserving the older helper's return shape. */
export async function fetchDocumentContent(
  documentId: string,
  signal?: AbortSignal,
): Promise<DocumentContent> {
  const access = await createDocumentAccessUrl(documentId, 'preview', signal);
  return {
    url: access.url,
    type: access.mime_type,
    expiresAt: access.expires_at,
    filename: access.filename,
    sizeBytes: access.size_bytes,
    supportsByteRanges: access.supports_byte_ranges,
    revoke: () => undefined,
  };
}

/** Mint a short-lived preview/download capability after a fresh visibility check. */
export function createDocumentAccessUrl(
  documentId: string,
  purpose: DocumentAccessPurpose,
  signal?: AbortSignal,
): Promise<DocumentAccessUrl> {
  return requestV2<DocumentAccessUrl>(`/documents/${documentId}/access-url`, {
    method: 'POST',
    json: { purpose },
    signal,
  });
}

/** Read one ordered transcript page, optionally positioned around a citation. */
export function fetchDocumentTranscript(
  documentId: string,
  query: TranscriptQuery = {},
  signal?: AbortSignal,
): Promise<TranscriptPage> {
  return requestV2<TranscriptPage>(
    `/documents/${documentId}/transcript${buildQuery({
      cursor: query.cursor,
      limit: query.limit,
      around_ms: query.around_ms,
    })}`,
    { signal },
  );
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
