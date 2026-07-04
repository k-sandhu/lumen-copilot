/**
 * Typed assistant calls — part of the api/ boundary (ADR-0004). The ONLY place
 * the SPA performs assistant HTTP. Conforms to the FROZEN contract
 * (contracts/openapi.yaml §assistants, ADR-0011 / #210):
 *
 *   GET    /assistants                    ?cursor&limit&status → AssistantList
 *   POST   /assistants                    {name, …}            → Assistant (201, draft)
 *   GET    /assistants/{id}                                    → Assistant
 *   PATCH  /assistants/{id}               {…}                  → Assistant
 *   DELETE /assistants/{id}                                    → 204
 *   POST   /assistants/{id}/publish       {notes?}             → AssistantVersion
 *   GET    /assistants/{id}/versions      ?cursor&limit        → AssistantVersionList
 *   POST   /assistants/{id}/rollback      {version, notes?}    → AssistantVersion
 *
 * Assistants are tenant- and owner/grant-scoped (spec 0004 INV-1/INV-2): a caller
 * only sees / mutates their own (or granted) assistants within their tenant.
 * Negative paths surface as typed `ApiError`s the UI branches on (problem body,
 * not status text): a missing/expired token → 401 (INV-4); a malformed body → 422
 * (INV-8); publishing without owner + backup owner → 422; a non-owned /
 * cross-tenant / unknown id → 404 (existence non-disclosure, INV-1/INV-2); a
 * rollback to an unknown/malformed `version` → 422.
 *
 * Version-history + rollback (GET /versions, POST /rollback) power the F-AB-4
 * (#214) versions panel — the append-only history is immutable (ADR-0011 §1): a
 * rollback creates a NEW head version equal to the target, never mutating history.
 */
import { request } from './client';
import type {
  Assistant,
  AssistantCreate,
  AssistantDraft,
  AssistantDraftRequest,
  AssistantList,
  AssistantPublishRequest,
  AssistantRollbackRequest,
  AssistantStatus,
  AssistantTestRequest,
  AssistantTestTrace,
  AssistantUpdate,
  AssistantVersion,
  AssistantVersionList,
} from './types';

/** Cursor-page + status-filter params for the list endpoint. */
export interface AssistantPageQuery {
  cursor?: string;
  limit?: number;
  status?: AssistantStatus;
}

/** Cursor-page params for the version-history endpoint. */
export interface AssistantVersionPageQuery {
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

/** List the caller's assistants (owned + shared), one cursor page. */
export function listAssistants(
  query: AssistantPageQuery = {},
  signal?: AbortSignal,
): Promise<AssistantList> {
  return request<AssistantList>(`/assistants${buildQuery({ ...query })}`, { signal });
}

/**
 * Create a draft assistant. Only `name` is required; the server owns
 * `owner`/`status`/timestamps. A malformed body → 422 (INV-8), surfaced inline.
 */
export function createAssistant(body: AssistantCreate): Promise<Assistant> {
  return request<Assistant>('/assistants', { method: 'POST', json: body });
}

/**
 * Draft an assistant config from a plain-language description (E6-1, #213). Creates
 * NOTHING — returns a draft config the editor pre-fills for review + save, plus
 * clarifying questions (missing scope/owner/risk), notes for any omitted tool/scope
 * (deny-by-default, INV-2), and warnings for any drafted high-risk tool. A blank /
 * oversize `description` → 422 (INV-8), surfaced inline.
 */
export function draftAssistant(body: AssistantDraftRequest): Promise<AssistantDraft> {
  return request<AssistantDraft>('/assistants/draft', { method: 'POST', json: body });
}

/** Get an assistant by id (404 if not yours / cross-tenant — INV-1/INV-2). */
export function getAssistant(id: string, signal?: AbortSignal): Promise<Assistant> {
  return request<Assistant>(`/assistants/${id}`, { signal });
}

/**
 * Patch a draft assistant's working head. At least one field must be present. A
 * malformed body → 422; a non-owned / cross-tenant id → 404.
 */
export function updateAssistant(id: string, body: AssistantUpdate): Promise<Assistant> {
  return request<Assistant>(`/assistants/${id}`, { method: 'PATCH', json: body });
}

/** Delete an assistant head (204). Non-owned / cross-tenant id → 404. */
export function deleteAssistant(id: string): Promise<void> {
  return request<void>(`/assistants/${id}`, { method: 'DELETE' });
}

/**
 * Publish a draft (draft → published). Requires both an owner and a distinct
 * backup owner — publishing without a backup owner → 422 (INV-8). Returns the
 * newly-frozen head version.
 */
export function publishAssistant(
  id: string,
  body: AssistantPublishRequest = {},
): Promise<AssistantVersion> {
  return request<AssistantVersion>(`/assistants/${id}/publish`, { method: 'POST', json: body });
}

/**
 * List an assistant's immutable published versions (newest first), one cursor
 * page (ADR-0011 §1). The history is append-only — publishing and rolling back
 * each append a version, nothing is ever mutated or deleted. A non-owned /
 * cross-tenant / unknown id → 404 (INV-1/INV-2).
 */
export function listAssistantVersions(
  id: string,
  query: AssistantVersionPageQuery = {},
  signal?: AbortSignal,
): Promise<AssistantVersionList> {
  return request<AssistantVersionList>(`/assistants/${id}/versions${buildQuery({ ...query })}`, {
    signal,
  });
}

/**
 * Roll the head back to a prior version (ADR-0011 §1). Creates a **new** head
 * version whose config copies the named prior version — history is never mutated
 * or deleted. An unknown / malformed `version` → 422; a non-owned / cross-tenant
 * id → 404. Returns the newly-created head version.
 */
export function rollbackAssistant(
  id: string,
  body: AssistantRollbackRequest,
): Promise<AssistantVersion> {
  return request<AssistantVersion>(`/assistants/${id}/rollback`, { method: 'POST', json: body });
}

/**
 * Preview/test/debug a draft assistant with NO real side effect (E6-5, #215).
 * Runs the assistant's working (draft) config against a sample input with write-tier
 * tools forced into simulate/deny mode (`write_file` simulated — no artifact;
 * `run_python` denied — no code run), and returns the debug trace (prompt, retrieval,
 * tool calls with args/results, outputs, errors, timing). A non-owned / cross-tenant
 * id → 404 (existence non-disclosure, INV-1/INV-2); a malformed body → 422.
 */
export function testAssistant(
  id: string,
  body: AssistantTestRequest = {},
): Promise<AssistantTestTrace> {
  return request<AssistantTestTrace>(`/assistants/${id}/test`, { method: 'POST', json: body });
}
