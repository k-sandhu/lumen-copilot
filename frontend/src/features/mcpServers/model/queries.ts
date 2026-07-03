/**
 * Server-state hooks for the MCP-servers slice (#228, ADR-0012) — TanStack Query
 * over the typed `@/api` boundary (the ONLY backend caller, ADR-0004). MCP
 * servers are server data: the list + per-server tools live in the query cache,
 * and the register/update/test/delete mutations invalidate them so the grid and
 * detail stay current. No transport here.
 *
 * Conforms to the FROZEN contract (contracts/openapi.yaml §mcp-servers): GET
 * /mcp-servers (cursor page), POST /mcp-servers (201 pending), GET
 * /mcp-servers/{id}, PATCH /mcp-servers/{id}, DELETE /mcp-servers/{id} (204),
 * POST /mcp-servers/{id}/test (200; status ready|error), GET
 * /mcp-servers/{id}/tools (cursor page). Negative paths surface as typed
 * `ApiError`s the components branch on (a 422 → inline form error, INV-8; a 404 →
 * existence non-disclosure, INV-1/INV-2; a 401 → re-auth, INV-4).
 */
import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseQueryResult,
} from '@tanstack/react-query';
import {
  deleteMcpServer,
  getMcpServer,
  listMcpServerTools,
  listMcpServers,
  registerMcpServer,
  testMcpServer,
  updateMcpServer,
} from '@/api';
import type {
  McpServer,
  McpServerCreate,
  McpServerList,
  McpServerUpdate,
  McpToolList,
} from '@/api';

/** Stable query keys for the slice. */
export const mcpKeys = {
  all: ['mcp-servers'] as const,
  list: () => [...mcpKeys.all, 'list'] as const,
  detail: (id: string) => [...mcpKeys.all, 'detail', id] as const,
  tools: (id: string) => [...mcpKeys.all, 'tools', id] as const,
};

/** The caller's registered MCP servers (the server grid). */
export function useMcpServers(): UseQueryResult<McpServerList> {
  return useQuery<McpServerList>({
    queryKey: mcpKeys.list(),
    queryFn: ({ signal }) => listMcpServers({}, signal),
    staleTime: 5_000,
  });
}

/** One MCP server's detail (the detail view target). Disabled until an id exists. */
export function useMcpServer(id: string | null): UseQueryResult<McpServer> {
  return useQuery<McpServer>({
    queryKey: mcpKeys.detail(id ?? '∅'),
    queryFn: ({ signal }) => getMcpServer(id as string, signal),
    enabled: id !== null,
    staleTime: 5_000,
  });
}

/** The tools discovered from a server on its last probe. Disabled until an id. */
export function useMcpServerTools(id: string | null): UseQueryResult<McpToolList> {
  return useQuery<McpToolList>({
    queryKey: mcpKeys.tools(id ?? '∅'),
    queryFn: ({ signal }) => listMcpServerTools(id as string, {}, signal),
    enabled: id !== null,
    staleTime: 5_000,
  });
}

/**
 * Register a server (AC-1). On success it comes back `pending`; we invalidate the
 * list so it appears immediately. A 422 (unsupported transport / blocked endpoint,
 * ADR-0012 §1/§4) propagates as an `ApiError` the register form renders inline —
 * it is NOT swallowed here.
 */
export function useRegisterMcpServer() {
  const qc = useQueryClient();
  return useMutation<McpServer, unknown, McpServerCreate>({
    mutationFn: (body) => registerMcpServer(body),
    onSuccess: () => void qc.invalidateQueries({ queryKey: mcpKeys.list() }),
  });
}

/**
 * Update a server (rename, toggle `enabled`, change endpoint, rotate the
 * write-only credential). Refreshes both the detail and the grid card. A 422 (bad
 * endpoint) / 404 propagates for the caller to surface.
 */
export function useUpdateMcpServer(id: string) {
  const qc = useQueryClient();
  return useMutation<McpServer, unknown, McpServerUpdate>({
    mutationFn: (body) => updateMcpServer(id, body),
    onSuccess: (updated) => {
      qc.setQueryData(mcpKeys.detail(id), updated);
      void qc.invalidateQueries({ queryKey: mcpKeys.list() });
    },
  });
}

/**
 * Probe a server's health + re-discover its tools (AC-1). The returned server
 * carries the new `status` (ready|error) — a down server returns 200 with
 * `status: error`, never throws (ADR-0012 §7), so the caller reads the outcome
 * from the result, not a rejected promise. Refreshes detail, tools, and the grid.
 */
export function useTestMcpServer(id: string) {
  const qc = useQueryClient();
  return useMutation<McpServer, unknown, void>({
    mutationFn: () => testMcpServer(id),
    onSuccess: (updated) => {
      qc.setQueryData(mcpKeys.detail(id), updated);
      void qc.invalidateQueries({ queryKey: mcpKeys.tools(id) });
      void qc.invalidateQueries({ queryKey: mcpKeys.list() });
    },
  });
}

/** Remove a server + its stored credential + discovered tools (204). Refreshes all. */
export function useDeleteMcpServer() {
  const qc = useQueryClient();
  return useMutation<void, unknown, string>({
    mutationFn: (id) => deleteMcpServer(id),
    onSuccess: () => void qc.invalidateQueries({ queryKey: mcpKeys.all }),
  });
}
