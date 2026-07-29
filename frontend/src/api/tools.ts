/**
 * Typed tool-catalogue call — part of the api/ boundary (ADR-0004). Conforms to the
 * frozen contract (contracts/openapi.yaml §tools, issue #505):
 *
 *   GET /tools → ToolCatalog
 *
 * Every registered tool with its static risk metadata plus the caller's tenant's
 * EFFECTIVE governance flags. This is the source the assistant builder composes a
 * `toolAllowlist` against; `/admin/tool-policy` carries the same data but is
 * admin-only (403 for an ordinary assistant owner, INV-5).
 *
 * Read-only: it GRANTS nothing. Changing a tenant's policy stays the admin write on
 * `PATCH /admin/tool-policy`, and the run-time approval gate is unchanged — this call
 * only lets the UI state those gates honestly. A missing/expired token surfaces as a
 * typed `ApiError` 401 (INV-4).
 */
import { request } from './client';
import type { ToolCatalog } from './types';

/** Every registered tool with this tenant's effective governance flags. */
export function listTools(signal?: AbortSignal): Promise<ToolCatalog> {
  return request<ToolCatalog>('/tools', { signal });
}
