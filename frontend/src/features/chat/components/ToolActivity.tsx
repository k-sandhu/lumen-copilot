/**
 * Surfaces retrieval-tool activity from the answer stream (AC-1): a "searching
 * documents…" indicator while a tool_call is running, and a compact summary once
 * the matching tool_result arrives ("searched documents — 3 passages"). Honors
 * prefers-reduced-motion via the StatusBadge pulse (CSS animate-pulse).
 */
import { StatusBadge } from '@/components/StatusBadge';
import type { ChatTool } from '@/api';
import type { ToolActivity as ToolActivityItem } from '../model/streamReducer';

const TOOL_LABEL: Record<ChatTool, string> = {
  search_text: 'Searching documents',
  search_documents: 'Looking up documents',
  list_documents: 'Listing documents',
  get_document: 'Reading a document',
  web_search: 'Searching the web',
};

function describe(item: ToolActivityItem): string {
  // A tool value outside the known union (a renamed/new backend tool ahead of a
  // types.ts update, a model-hallucinated name, or a persisted governed tool —
  // run_python, MCP tools — #377) must not render the literal "undefined…".
  // Fall back to the actual tool name (honest), guarding the empty case (#280).
  const label = TOOL_LABEL[item.tool] ?? (item.tool || 'Tool');
  if (item.status === 'running') return `${label}…`;
  if (item.summary) return `${label} — ${item.summary}`;
  const n = item.hitCount ?? 0;
  return `${label} — ${n} ${n === 1 ? 'passage' : 'passages'}`;
}

export function ToolActivity({ tools }: { tools: ToolActivityItem[] }) {
  if (tools.length === 0) return null;
  return (
    // "Tool activity", not "Retrieval activity" (#397): this list renders the
    // whole governed registry — run_python, MCP tools, denials — not only
    // retrieval, and the accessible name must say so.
    <ul className="mb-2 flex flex-wrap gap-1.5" aria-label="Tool activity">
      {tools.map((item) => {
        const running = item.status === 'running';
        // A persisted governance denial / tool failure (#377) is a danger badge —
        // visible, never silently dropped. The live stream leaves `ok` unset.
        const failed = item.ok === false;
        return (
          <li key={item.callId}>
            <StatusBadge
              tone={running ? 'pending' : failed ? 'danger' : 'ok'}
              pulse={running}
            >
              {describe(item)}
            </StatusBadge>
          </li>
        );
      })}
    </ul>
  );
}
