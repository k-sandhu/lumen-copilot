/**
 * The tool allow-list source for the assistant builder (#212, ADR-0011 §1).
 *
 * TODO(F-ADMIN-TOOLS #223 / CC-A #207): there is NO tool-registry endpoint in the
 * frozen contract yet — the assistant surface must drive its tool allow-list from
 * the tenant's actual registered tools once `GET /admin/tools` (or equivalent)
 * lands. Until then this is a small STATIC stub of the known built-in tool names
 * (the default retrieval tool set the runtime already exposes on the chat WS,
 * `ChatTool` in api/types.ts), so builders can compose an allow-list against a
 * stable, contract-shaped list. When the endpoint ships, swap `KNOWN_TOOLS` for a
 * TanStack Query over it and delete this note — the `toolAllowlist: string[]` wire
 * shape does not change, only where the candidate names come from.
 *
 * An empty allow-list on the wire (`[]`) means "the default retrieval tool set"
 * (per the contract), so an assistant with nothing ticked is still useful.
 */

/** One selectable tool: its wire name + a human label/summary for the checkbox. */
export interface ToolOption {
  /** The wire name persisted into `toolAllowlist` (matches `ChatTool`). */
  name: string;
  label: string;
  description: string;
}

/**
 * The known built-in tools. These mirror the retrieval tool set the chat runtime
 * already exposes (`ChatTool`), so an allow-list built here is meaningful today.
 */
export const KNOWN_TOOLS: readonly ToolOption[] = [
  {
    name: 'search_text',
    label: 'Text search',
    description: 'Keyword / full-text search across the assistant’s permitted sources.',
  },
  {
    name: 'search_documents',
    label: 'Document search',
    description: 'Semantic retrieval over the assistant’s permitted documents.',
  },
  {
    name: 'get_document',
    label: 'Read document',
    description: 'Fetch the full text of a permitted document by id for grounding.',
  },
] as const;
