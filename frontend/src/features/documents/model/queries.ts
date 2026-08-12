/**
 * Server-state hooks for the documents slice — TanStack Query (#49). Collections
 * and documents are server data; mutations invalidate the relevant query keys so
 * lists stay fresh. The only client-side state (in-flight upload progress) lives
 * in the upload store, not here (frontend/AGENTS.md: don't mirror server data).
 *
 * Conforms to the frozen contract via the api/ boundary only — no transport here.
 *
 * STATUS POLLING (AC-2): the documents list refetches on an interval WHILE any
 * document is still ingesting (pending|processing) and goes quiet once everything
 * is settled (ready|failed) — so the pending→processing→ready→failed transitions
 * surface without hammering the backend.
 */
import {
  keepPreviousData,
  useQuery,
  useQueryClient,
  type UseQueryResult,
} from '@tanstack/react-query';
import { usePrincipalMutation } from '@/lib/usePrincipalMutation';
import {
  createCollection,
  deleteCollection,
  deleteDocument,
  listCollections,
  listDocuments,
  updateCollection,
} from '@/api';
import type {
  Collection,
  CollectionCreate,
  CollectionList,
  CollectionUpdate,
  Document,
  DocumentList,
  DocumentListQuery,
} from '@/api';

/** How often to re-poll while documents are still ingesting (ms). */
export const INGEST_POLL_INTERVAL_MS = 2_500;

export const collectionsKey = ['collections'] as const;
export const documentsKey = (filters: DocumentFilters) =>
  ['documents', filters.collectionId ?? null, filters.status ?? null, filters.q ?? null] as const;

export interface DocumentFilters {
  collectionId?: string;
  status?: Document['status'];
  /** Filename substring filter. */
  q?: string;
}

// --- Collections ----------------------------------------------------------

/** The caller's collections (AC-1). */
export function useCollections(): UseQueryResult<CollectionList> {
  return useQuery<CollectionList>({
    queryKey: collectionsKey,
    queryFn: ({ signal }) => listCollections({}, signal),
    staleTime: 10_000,
  });
}

/** Create a collection (AC-1). Invalidates the collections list on success. */
export function useCreateCollection() {
  const qc = useQueryClient();
  return usePrincipalMutation<Collection, unknown, CollectionCreate>({
    mutationFn: (body) => createCollection(body),
    onSuccess: () => void qc.invalidateQueries({ queryKey: collectionsKey }),
  });
}

/** Rename / re-describe a collection (AC-1). */
export function useUpdateCollection() {
  const qc = useQueryClient();
  return usePrincipalMutation<Collection, unknown, { id: string; body: CollectionUpdate }>({
    mutationFn: ({ id, body }) => updateCollection(id, body),
    onSuccess: () => void qc.invalidateQueries({ queryKey: collectionsKey }),
  });
}

/** Delete a collection and its documents (AC-1). Refreshes collections + docs. */
export function useDeleteCollection() {
  const qc = useQueryClient();
  return usePrincipalMutation<void, unknown, string>({
    mutationFn: (id) => deleteCollection(id),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: collectionsKey });
      void qc.invalidateQueries({ queryKey: ['documents'] });
    },
  });
}

// --- Documents ------------------------------------------------------------

function hasUnsettled(list: DocumentList | undefined): boolean {
  return (list?.items ?? []).some((d) => d.status === 'pending' || d.status === 'processing');
}

/**
 * Documents in a collection, filtered by status / filename (AC-3). Polls on an
 * interval while anything is still ingesting and stops once settled (AC-2).
 * Disabled until a collection is selected — there is no "all documents" view.
 */
export function useDocuments(filters: DocumentFilters): UseQueryResult<DocumentList> {
  const query: DocumentListQuery = {
    collection_id: filters.collectionId,
    status: filters.status,
    q: filters.q,
  };
  return useQuery<DocumentList>({
    queryKey: documentsKey(filters),
    queryFn: ({ signal }) => listDocuments(query, signal),
    enabled: Boolean(filters.collectionId),
    staleTime: 0,
    // Keep the previous rows on screen while a refined filter/status loads, so
    // typing in the filename filter never flashes the whole table to skeletons
    // on every keystroke (#272) — matching useSearch / useAuditEvents.
    placeholderData: keepPreviousData,
    refetchInterval: (q) => (hasUnsettled(q.state.data) ? INGEST_POLL_INTERVAL_MS : false),
  });
}

/** Delete a document and its chunks/embeddings (AC-3). Refreshes the doc lists. */
export function useDeleteDocument() {
  const qc = useQueryClient();
  return usePrincipalMutation<void, unknown, string>({
    mutationFn: (id) => deleteDocument(id),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['documents'] });
      void qc.invalidateQueries({ queryKey: collectionsKey });
    },
  });
}

/** Invalidate the document lists + collection counts (after an upload completes). */
export function useRefreshDocuments() {
  const qc = useQueryClient();
  return () => {
    void qc.invalidateQueries({ queryKey: ['documents'] });
    void qc.invalidateQueries({ queryKey: collectionsKey });
  };
}
