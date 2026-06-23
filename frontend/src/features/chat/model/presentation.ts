/**
 * Pure presentation helpers for the chat trust-signal re-skin (#89). No I/O, no
 * React — they derive the wireframe trust signals (DESIGN.md §1/§6, ADR-0007 §1)
 * from the chat/citation data the slice ALREADY has (no contract change): the
 * retrieval trace, freshness labels, the model badge label, and the
 * SourceInspector passage. Keeping the derivation out of JSX makes every edge
 * (zero citations, no tools, missing timestamps) unit-testable.
 */
import type { TraceStep } from '@/ui';
import type { SourcePassage } from '@/ui';
import type { UiCitation } from './citation';
import type { ToolActivity } from './streamReducer';

/** Compact thousands formatting, e.g. 1204 → "1,204". */
function groupThousands(n: number): string {
  return n.toLocaleString('en-US');
}

/**
 * Relative-time label from an ISO timestamp, e.g. "2h ago" / "Yesterday".
 * `now` is injectable so the output is deterministic in tests. Returns null for
 * an absent/unparseable timestamp so the caller can omit the pill entirely.
 */
export function relativeTime(iso: string | undefined, now: number = Date.now()): string | null {
  if (!iso) return null;
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return null;
  const seconds = Math.max(0, Math.round((now - then) / 1000));
  if (seconds < 45) return 'Just now';
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.round(hours / 24);
  if (days === 1) return 'Yesterday';
  if (days < 7) return `${days}d ago`;
  const weeks = Math.round(days / 7);
  if (weeks < 5) return `${weeks}w ago`;
  const months = Math.round(days / 30);
  if (months < 12) return `${months}mo ago`;
  return `${Math.round(days / 365)}y ago`;
}

/**
 * A source is "stale" when its freshest evidence is older than this window
 * (mission "freshness" — surface evidence age, never hide it). 30 days.
 */
export const STALE_AFTER_MS = 30 * 24 * 60 * 60 * 1000;

export function isStale(iso: string | undefined, now: number = Date.now()): boolean {
  if (!iso) return false;
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return false;
  return now - then > STALE_AFTER_MS;
}

/**
 * The model badge label. The chat data carries a raw model id
 * (e.g. "anthropic/claude-opus-4.8"); the picker `label` is the friendly name.
 * Prefer the friendly label when we have one, else the trailing id segment.
 */
export function modelBadgeLabel(
  modelId: string | undefined,
  friendly?: string | undefined,
): string | null {
  if (friendly && friendly.trim()) return friendly;
  if (!modelId) return null;
  const tail = modelId.split('/').pop() ?? modelId;
  return tail;
}

export interface RetrievalSummary {
  /** One-line header, e.g. "Looked at 3 sources · 1,204 passages · 38 excluded". */
  summary: string;
  steps: TraceStep[];
  /** Whether there is anything worth showing (drives whether the trace renders). */
  hasContent: boolean;
}

/**
 * Build the RetrievalTrace from the data the turn already has: the distinct
 * cited documents (sources), the retrieval-tool hit counts (passages searched),
 * and — when present — an excluded count. Excluded candidates render muted so a
 * permission trim is visible, not hidden (mission filter #4).
 */
export function buildRetrievalSummary(
  citations: UiCitation[],
  tools: ToolActivity[],
  excludedCount = 0,
): RetrievalSummary {
  const sourceCount = new Set(citations.map((c) => c.documentId)).size;
  const passageCount = tools.reduce((sum, t) => sum + (t.hitCount ?? 0), 0);

  const parts: string[] = [`${sourceCount} ${sourceCount === 1 ? 'source' : 'sources'}`];
  if (passageCount > 0) {
    parts.push(`${groupThousands(passageCount)} ${passageCount === 1 ? 'passage' : 'passages'}`);
  }
  if (excludedCount > 0) parts.push(`${groupThousands(excludedCount)} excluded`);

  const steps: TraceStep[] = [];
  for (const tool of tools) {
    if (tool.status !== 'done') continue;
    const n = tool.hitCount ?? 0;
    const label = tool.summary
      ? `Searched sources — ${tool.summary}`
      : `Searched sources — ${groupThousands(n)} ${n === 1 ? 'passage' : 'passages'}`;
    steps.push({ label });
  }
  if (sourceCount > 0) {
    steps.push({ label: `Cited ${sourceCount} ${sourceCount === 1 ? 'source' : 'sources'}` });
  }
  if (excludedCount > 0) {
    steps.push({
      label: `${groupThousands(excludedCount)} results excluded — you don't have access`,
      excluded: true,
    });
  }

  return {
    summary: `Looked at ${parts.join(' · ')}`,
    steps,
    hasContent: sourceCount > 0 || passageCount > 0 || excludedCount > 0,
  };
}

/**
 * Split a citation snippet into highlight runs for the SourceInspector. We
 * don't get sub-span offsets within the snippet from the wire, so the whole
 * cited passage is the highlight — which is exactly what was cited.
 */
export function passageFromCitation(citation: UiCitation): SourcePassage {
  const text = citation.snippet.trim();
  if (!text) return { runs: [] };
  return { runs: [{ text, highlight: true }] };
}
