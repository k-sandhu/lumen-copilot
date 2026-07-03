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
  get_document: 'Reading a document',
  web_search: 'Searching the web',
};

function describe(item: ToolActivityItem): string {
  // A tool value outside the known union (a renamed/new backend tool ahead of a
  // types.ts update, or a model-hallucinated name) must not render the literal
  // "undefined…" — fall back to a generic, honest label (#280).
  const label = TOOL_LABEL[item.tool] ?? 'Searching sources';
  if (item.status === 'running') return `${label}…`;
  if (item.summary) return `${label} — ${item.summary}`;
  const n = item.hitCount ?? 0;
  return `${label} — ${n} ${n === 1 ? 'passage' : 'passages'}`;
}

export function ToolActivity({ tools }: { tools: ToolActivityItem[] }) {
  if (tools.length === 0) return null;
  return (
    <ul className="mb-2 flex flex-wrap gap-1.5" aria-label="Retrieval activity">
      {tools.map((item) => {
        const running = item.status === 'running';
        return (
          <li key={item.callId}>
            <StatusBadge tone={running ? 'pending' : 'ok'} pulse={running}>
              {describe(item)}
            </StatusBadge>
          </li>
        );
      })}
    </ul>
  );
}
