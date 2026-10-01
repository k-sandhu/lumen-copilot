/**
 * Server-state hooks for the Assistants slice (#212, ADR-0011) — TanStack Query
 * over the typed `@/api` boundary (the ONLY backend caller, ADR-0004). Assistants
 * are server data: lists/detail live in the query cache and the create/update/
 * publish/delete mutations invalidate them so the library + editor stay current.
 * No transport here.
 *
 * Conforms to the FROZEN contract (contracts/openapi.yaml §assistants): GET
 * /assistants (cursor page + status filter), POST /assistants (201 draft), GET
 * /assistants/{id}, PATCH /assistants/{id}, POST /assistants/{id}/publish, DELETE
 * /assistants/{id}. Negative paths surface as typed `ApiError`s the components
 * branch on (a 422 → inline form/publish error, INV-8; a 404 → existence
 * non-disclosure, INV-1/INV-2; a 401 → re-auth dead-end, INV-4).
 *
 * The editor also reads the reference lists it composes an assistant from — the
 * model registry (GET /models), collections (GET /collections), sources (GET
 * /sources), and the tenant roster for owner/backup pickers (GET /admin/members,
 * admin-only → a non-admin caller gets 403 which we surface honestly). Those are
 * plain reads cached generously; they never mutate here.
 */
import {
  useInfiniteQuery,
  useQuery,
  useQueryClient,
  type UseInfiniteQueryResult,
  type UseQueryResult,
} from '@tanstack/react-query';
import { usePrincipalMutation } from '@/lib/usePrincipalMutation';
import {
  createAssistant,
  deleteAssistant,
  draftAssistant,
  getAssistant,
  listAssistantVersions,
  listAssistants,
  listCollections,
  listMembers,
  listSources,
  listTools,
  publishAssistant,
  rollbackAssistant,
  testAssistant,
  updateAssistant,
} from '@/api';
import type {
  Assistant,
  AssistantCreate,
  AssistantDraft,
  AssistantDraftRequest,
  AssistantList,
  AssistantPublishRequest,
  AssistantRollbackRequest,
  AssistantTestRequest,
  AssistantTestTrace,
  AssistantUpdate,
  AssistantVersion,
  AssistantVersionList,
  CollectionList,
  MemberList,
  SourceList,
  ToolCatalog,
} from '@/api';

/** Stable query keys for the slice. */
export const assistantKeys = {
  all: ['assistants'] as const,
  list: () => [...assistantKeys.all, 'list'] as const,
  detail: (id: string) => [...assistantKeys.all, 'detail', id] as const,
  versions: (id: string) => [...assistantKeys.all, 'versions', id] as const,
};

/** The caller's assistants (owned + shared) — the library grid. */
export function useAssistants(): UseQueryResult<AssistantList> {
  return useQuery<AssistantList>({
    queryKey: assistantKeys.list(),
    queryFn: ({ signal }) => listAssistants({}, signal),
    staleTime: 5_000,
  });
}

/** One assistant's mutable head (the editor target). Disabled until an id exists. */
export function useAssistant(id: string | null): UseQueryResult<Assistant> {
  return useQuery<Assistant>({
    queryKey: assistantKeys.detail(id ?? '∅'),
    queryFn: ({ signal }) => getAssistant(id as string, signal),
    enabled: id !== null,
  });
}

/** Create a draft assistant. Invalidates the library so it appears immediately. */
export function useCreateAssistant() {
  const qc = useQueryClient();
  return usePrincipalMutation<Assistant, unknown, AssistantCreate>({
    mutationFn: (body) => createAssistant(body),
    onSuccess: () => void qc.invalidateQueries({ queryKey: assistantKeys.list() }),
  });
}

/**
 * Draft an assistant config from a plain-language description (E6-1, #213). This
 * creates NOTHING on the server — it returns a suggested config + clarifications
 * the "Describe your assistant" surface loads into the editor for review. So it does
 * NOT invalidate the library (nothing was created); the actual create happens when
 * the user saves the pre-filled editor. A 422 (blank/oversize description)
 * propagates as an `ApiError` the caller renders inline — it is NOT swallowed here.
 */
export function useDraftAssistant() {
  return usePrincipalMutation<AssistantDraft, unknown, AssistantDraftRequest>({
    mutationFn: (body) => draftAssistant(body),
  });
}

/** Patch the working head. Refreshes both the detail and the library card. */
export function useUpdateAssistant(id: string) {
  const qc = useQueryClient();
  return usePrincipalMutation<Assistant, unknown, AssistantUpdate>({
    mutationFn: (body) => updateAssistant(id, body),
    onSuccess: (updated) => {
      qc.setQueryData(assistantKeys.detail(id), updated);
      void qc.invalidateQueries({ queryKey: assistantKeys.list() });
    },
  });
}

/**
 * Publish a draft (draft → published). A 422 (missing owner + backup owner, or a
 * malformed body) propagates as an `ApiError` the editor renders inline — it is
 * NOT swallowed here. On success the head detail + library are refreshed so the
 * new `published` status + version surface, and the version history is
 * invalidated so the freshly-frozen version appears in the panel (#214).
 */
export function usePublishAssistant(id: string) {
  const qc = useQueryClient();
  return usePrincipalMutation<AssistantVersion, unknown, AssistantPublishRequest>({
    mutationFn: (body) => publishAssistant(id, body),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: assistantKeys.detail(id) });
      void qc.invalidateQueries({ queryKey: assistantKeys.list() });
      void qc.invalidateQueries({ queryKey: assistantKeys.versions(id) });
    },
  });
}

/**
 * The assistant's immutable version history (#214, ADR-0011 §1) — newest first,
 * cursor-paginated as an infinite query so the panel can "Load more". The history
 * is append-only: publishing and rolling back each append a version. Disabled
 * until an id exists (never fired for `/assistants/new`).
 */
export function useAssistantVersions(
  id: string | null,
): UseInfiniteQueryResult<{ pages: AssistantVersionList[] }, unknown> {
  return useInfiniteQuery({
    queryKey: assistantKeys.versions(id ?? '∅'),
    queryFn: ({ pageParam, signal }) =>
      listAssistantVersions(id as string, { cursor: pageParam }, signal),
    enabled: id !== null,
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last: AssistantVersionList) => last.next_cursor ?? undefined,
    staleTime: 5_000,
  });
}

/**
 * Roll the head back to a prior version (#214, ADR-0011 §1). Creates a NEW head
 * version equal to the target — history is never mutated. A 422 (unknown/malformed
 * `version`) propagates as an `ApiError` the panel renders inline. On success the
 * head detail + library + version history are all refreshed so the new head
 * version and updated `version` pointer surface.
 */
export function useRollbackAssistant(id: string) {
  const qc = useQueryClient();
  return usePrincipalMutation<AssistantVersion, unknown, AssistantRollbackRequest>({
    mutationFn: (body) => rollbackAssistant(id, body),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: assistantKeys.detail(id) });
      void qc.invalidateQueries({ queryKey: assistantKeys.list() });
      void qc.invalidateQueries({ queryKey: assistantKeys.versions(id) });
    },
  });
}

/**
 * Run a read-only test/preview of a draft assistant (#215, E6-5). Returns the debug
 * trace (prompt, retrieval, tool calls, outputs, timing) with NO real side effect —
 * write-tier tools simulate/deny, nothing is persisted. A 404 (non-owned/cross-tenant)
 * or 422 (malformed) propagates as a typed `ApiError` the panel renders inline; it is
 * NOT swallowed. Nothing is invalidated — a preview mutates no server state, so the
 * library/detail caches are untouched.
 */
export function useTestAssistant(id: string) {
  return usePrincipalMutation<AssistantTestTrace, unknown, AssistantTestRequest>({
    mutationFn: (body) => testAssistant(id, body),
  });
}

/** Delete an assistant head (204). Refreshes the library. */
export function useDeleteAssistant() {
  const qc = useQueryClient();
  return usePrincipalMutation<void, unknown, string>({
    mutationFn: (id) => deleteAssistant(id),
    onSuccess: () => void qc.invalidateQueries({ queryKey: assistantKeys.all }),
  });
}

// --- Reference lists the editor + library compose against -------------------

/**
 * The model-picker registry (GET /models) — re-exported from the shared slice so
 * this feature keeps its single-import surface while the query (and its cache
 * key) has exactly ONE definition (FE-9). It used to be a byte-identical copy of
 * chat's, which is how the same key ended up declared in three places.
 */
export { useModels } from '@/features/models';

/** The caller's collections (knowledge-scope picker). */
export function useCollections(): UseQueryResult<CollectionList> {
  return useQuery<CollectionList>({
    queryKey: ['collections'],
    queryFn: ({ signal }) => listCollections({}, signal),
    staleTime: 30_000,
  });
}

/** The caller's connected sources (knowledge-scope picker). */
export function useSources(): UseQueryResult<SourceList> {
  return useQuery<SourceList>({
    queryKey: ['sources'],
    queryFn: ({ signal }) => listSources({}, signal),
    staleTime: 30_000,
  });
}

/**
 * The tenant roster for the owner + backup-owner pickers (GET /admin/members).
 * Admin-only per the contract: a non-admin caller receives 403 (INV-5), which the
 * pickers surface as an honest "roster unavailable" note rather than a blank list.
 */
export function useMembers(): UseQueryResult<MemberList> {
  return useQuery<MemberList>({
    queryKey: ['admin', 'members'],
    queryFn: ({ signal }) => listMembers({}, signal),
    staleTime: 60_000,
  });
}

/**
 * The tool catalogue for the allow-list picker (GET /tools, issue #505): every
 * registered tool with its risk tier and the tenant's effective governance flags.
 * Member-readable by design — the equivalent admin surface (`/admin/tool-policy`)
 * 403s for an ordinary assistant owner, which is why the builder used to render a
 * hardcoded stub that omitted `run_python` entirely.
 *
 * Cached generously: the registry and the tenant's policy both change rarely, and a
 * governance change lands on the next mount. The read grants nothing; the tenant
 * policy and the run-time approval gate remain the enforcement points.
 */
export function useToolCatalog(): UseQueryResult<ToolCatalog> {
  return useQuery<ToolCatalog>({
    queryKey: ['tools', 'catalog'],
    queryFn: ({ signal }) => listTools(signal),
    staleTime: 60_000,
  });
}
