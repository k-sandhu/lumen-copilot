/**
 * Transcript + citation presentation for the run-detail view (#237, AC-2). Pure
 * mappers from the wire `RunStep[]` / `Citation` to display-ready shapes and the
 * kit's `RetrievalTrace` / `SourceInspector` inputs — kept out of the JSX so the
 * component stays presentational and this is unit-testable.
 *
 * A run's persisted `steps` are the durable analogue of the WS envelope stream
 * (ADR-0015 §2): `delta` text, `tool_call` / `tool_result` turns, `citation`
 * passages, and `error` steps, ordered by `seq`.
 */
import type { SourcePassage } from '@/ui';
import type { Citation, RunStep } from '@/api';

/** A concatenated assistant answer, assembled from the ordered `delta` steps. */
export function assembleAnswer(steps: RunStep[] | undefined): string {
  if (!steps) return '';
  return steps
    .filter((s) => s.kind === 'delta')
    .sort((a, b) => a.seq - b.seq)
    .map((s) => extractText(s.payload))
    .join('');
}

/** Best-effort text extraction from a delta step payload. */
function extractText(payload: Record<string, unknown> | undefined): string {
  if (!payload) return '';
  const text = payload.text ?? payload.content ?? payload.delta;
  return typeof text === 'string' ? text : '';
}

/** A display-ready transcript step. */
export interface TranscriptItem {
  seq: number;
  kind: RunStep['kind'];
  /** A short label, e.g. "Called search" or "Passage cited". */
  label: string;
  /** A monospace detail (tool args/result, error code), when present. */
  detail?: string;
}

const TOOL_NAME_KEYS = ['name', 'tool', 'tool_name'] as const;

/** Map the ordered steps to display items (tool calls, citations, errors). */
export function toTranscript(steps: RunStep[] | undefined): TranscriptItem[] {
  if (!steps) return [];
  return [...steps]
    .sort((a, b) => a.seq - b.seq)
    .filter((s) => s.kind !== 'delta')
    .map((s) => {
      switch (s.kind) {
        case 'tool_call':
          return {
            seq: s.seq,
            kind: s.kind,
            label: `Called ${toolName(s.payload) ?? 'a tool'}`,
            detail: payloadDetail(s.payload),
          };
        case 'tool_result':
          return {
            seq: s.seq,
            kind: s.kind,
            label: `Result from ${toolName(s.payload) ?? 'a tool'}`,
            detail: payloadDetail(s.payload),
          };
        case 'citation':
          return {
            seq: s.seq,
            kind: s.kind,
            label: 'Passage cited',
            detail: citationSnippet(s.payload),
          };
        case 'error':
          return { seq: s.seq, kind: s.kind, label: 'Error', detail: payloadDetail(s.payload) };
        default:
          return { seq: s.seq, kind: s.kind, label: s.kind };
      }
    });
}

function toolName(payload: Record<string, unknown> | undefined): string | undefined {
  if (!payload) return undefined;
  for (const key of TOOL_NAME_KEYS) {
    const v = payload[key];
    if (typeof v === 'string' && v.length > 0) return v;
  }
  return undefined;
}

function citationSnippet(payload: Record<string, unknown> | undefined): string | undefined {
  const snippet = payload?.snippet;
  return typeof snippet === 'string' ? snippet : undefined;
}

function payloadDetail(payload: Record<string, unknown> | undefined): string | undefined {
  if (!payload || Object.keys(payload).length === 0) return undefined;
  try {
    return JSON.stringify(payload, null, 2);
  } catch {
    return undefined;
  }
}

/**
 * Split a citation's snippet into a highlighted `SourcePassage` for the kit's
 * SourceInspector. The snippet IS the cited passage; `char_start`/`char_end` are
 * document-absolute offsets, so we highlight the whole snippet run (the passage
 * the answer used), matching how chat renders a citation.
 */
export function citationToPassage(citation: Citation): SourcePassage {
  return { runs: [{ text: citation.snippet, highlight: true }] };
}

/** A count summary for the RetrievalTrace header, e.g. "2 tool calls · 3 citations". */
export function traceSummary(
  steps: RunStep[] | undefined,
  citations: Citation[] | undefined,
): string {
  const toolCalls = (steps ?? []).filter((s) => s.kind === 'tool_call').length;
  const citeCount = citations?.length ?? 0;
  const parts: string[] = [];
  parts.push(`${toolCalls} tool call${toolCalls === 1 ? '' : 's'}`);
  parts.push(`${citeCount} citation${citeCount === 1 ? '' : 's'}`);
  return parts.join(' · ');
}
