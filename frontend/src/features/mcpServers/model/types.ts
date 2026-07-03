/**
 * Local type surface for the MCP-servers slice (#228, ADR-0012) — re-exports the
 * FROZEN wire types from the api/ boundary (never re-declared here;
 * frontend/AGENTS.md "don't re-declare wire shapes") plus the kit's StatusTone,
 * so presentation helpers and components import their types from one slice-local
 * module.
 */
export type {
  McpServer,
  McpServerList,
  McpServerCreate,
  McpServerUpdate,
  McpServerAuth,
  McpServerStatus,
  McpTransport,
  McpRiskTier,
  McpTool,
  McpToolList,
} from '@/api';
export type { StatusTone } from '@/ui';
