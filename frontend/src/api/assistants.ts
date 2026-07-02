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
 *
 * Assistants are tenant- and owner/grant-scoped (spec 0004 INV-1/INV-2): a caller
 * only sees / mutates their own (or granted) assistants within their tenant.
 * Negative paths surface as typed `ApiError`s the UI branches on (problem body,
 * not status text): a missing/expired token → 401 (INV-4); a malformed body → 422
 * (INV-8); publishing without owner + backup owner → 422; a non-owned /
 * cross-tenant / unknown id → 404 (existence non-disclosure, INV-1/INV-2).
 *
 * Version-history + rollback (GET /versions, POST /rollback) are deliberately out
 * of scope for #212 (they belong to F-AB-4); this client covers only the
 * builder + library surface.
 */
import { request } from './client';
import type {
  Assistant,
  AssistantCreate,
  AssistantList,
  AssistantPublishRequest,
  AssistantStatus,
  AssistantUpdate,
  AssistantVersion,
} from './types';

/** Cursor-page + status-filter params for the list endpoint. */
export interface AssistantPageQuery {
  cursor?: string;
  limit?: number;
  status?: AssistantStatus;
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
