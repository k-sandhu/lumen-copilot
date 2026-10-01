/**
 * Typed MCP-server calls — part of the api/ boundary (ADR-0004). The ONLY place
 * the SPA performs HTTP for the MCP management slice. Conforms to the FROZEN
 * contract (contracts/openapi.yaml §mcp-servers, ADR-0012 / #224):
 *
 *   GET    /mcp-servers              ?cursor&limit → McpServerList
 *   POST   /mcp-servers              {name, transport, endpoint_url, auth?} → McpServer (201, pending)
 *   GET    /mcp-servers/{id}                       → McpServer
 *   PATCH  /mcp-servers/{id}         {…}            → McpServer
 *   DELETE /mcp-servers/{id}                        → 204
 *   POST   /mcp-servers/{id}/test                   → McpServer (200; status ready|error)
 *   GET    /mcp-servers/{id}/tools   ?cursor&limit  → McpToolList
 *
 * MCP servers are tenant- and owner-scoped (spec 0004 INV-1/INV-2): a caller only
 * sees / mutates their own servers within their tenant. The stored credential is
 * WRITE-ONLY — sent in the register/update `auth` field, never returned; the
 * response carries only a masked `secret_hint` (ADR-0012 §5, CC-C #209).
 *
 * Negative paths surface as typed `ApiError`s the UI branches on (problem body,
 * not status text): a missing/expired token → 401 (INV-4); a malformed body, an
 * unsupported/`stdio` transport, or an invalid / non-https / SSRF-blocked
 * `endpoint_url` → 422 (INV-8, `code` distinguishes: `endpoint_blocked`,
 * `unsupported_transport`); a non-owner / cross-tenant id → 404 (existence
 * non-disclosure, INV-1/INV-2). A `test` never throws for a down server — a
 * failed handshake returns 200 with `status: error` + `last_error` (ADR-0012 §7).
 */
import { request } from './client';
import type {
  McpServer,
  McpServerCreate,
  McpServerList,
  McpServerUpdate,
  McpToolList,
} from './types';

/** Cursor-page params for the list endpoints. */
export interface McpPageQuery {
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

/** List the caller's registered MCP servers (one cursor page). */
export function listMcpServers(
  page: McpPageQuery = {},
  signal?: AbortSignal,
): Promise<McpServerList> {
  return request<McpServerList>(`/mcp-servers${buildQuery({ ...page })}`, { signal });
}

/**
 * Register a remote MCP server. The server validates the transport + https/SSRF
 * of `endpoint_url` (an unsupported transport or blocked URL → 422) and stores
 * any `auth` write-only via CC-C; the returned server has status `pending` until
 * the first `test` probe.
 */
export function registerMcpServer(body: McpServerCreate, signal?: AbortSignal): Promise<McpServer> {
  return request<McpServer>('/mcp-servers', { method: 'POST', json: body, signal });
}

/** Get one MCP server by id (404 if not yours / cross-tenant — INV-1/INV-2). */
export function getMcpServer(id: string, signal?: AbortSignal): Promise<McpServer> {
  return request<McpServer>(`/mcp-servers/${id}`, { signal });
}

/**
 * Update a registered MCP server (rename, toggle `enabled`, change `endpoint_url`,
 * or rotate the write-only `auth` credential — send `auth: null` to clear). At
 * least one field. A malformed body / blocked URL → 422; a non-owned id → 404.
 */
export function updateMcpServer(id: string, body: McpServerUpdate): Promise<McpServer> {
  return request<McpServer>(`/mcp-servers/${id}`, { method: 'PATCH', json: body });
}

/**
 * Remove a registered MCP server, its stored credential, and its discovered-tool
 * snapshot (204). A non-owned / cross-tenant id → 404 (INV-1/INV-2).
 */
export function deleteMcpServer(id: string): Promise<void> {
  return request<void>(`/mcp-servers/${id}`, { method: 'DELETE' });
}

/**
 * Probe a server's health and re-discover its tools. Returns the UPDATED server:
 * a healthy probe → `status: ready` (+ refreshed `last_health_at` /
 * `discovered_tool_count`); an unreachable/erroring probe → `status: error` (+ a
 * safe `last_error`). BOTH outcomes return 200 — the result is in `status`, not
 * the HTTP code (ADR-0012 §7). A non-owned id → 404.
 */
export function testMcpServer(id: string): Promise<McpServer> {
  return request<McpServer>(`/mcp-servers/${id}/test`, { method: 'POST' });
}

/**
 * List the tools discovered from a server on its last successful probe (one
 * cursor page). Each tool carries its namespaced registry name, description, the
 * advertised argument JSON schema, and an inferred `risk_tier`. A non-owned id →
 * 404.
 */
export function listMcpServerTools(
  id: string,
  page: McpPageQuery = {},
  signal?: AbortSignal,
): Promise<McpToolList> {
  return request<McpToolList>(`/mcp-servers/${id}/tools${buildQuery({ ...page })}`, { signal });
}
