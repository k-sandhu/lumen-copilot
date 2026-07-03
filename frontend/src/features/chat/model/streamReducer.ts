/**
 * Pure reducer that folds the chat answer-stream WS envelopes into renderable UI
 * state. Kept pure (no React, no socket) so the full stream lifecycle — including
 * the terminal `error` and citation/tool side-band events — is unit-testable in
 * isolation (root §9; frontend/AGENTS.md "every state").
 *
 * Lifecycle (websocket-envelopes.schema.json x-chatStream):
 *   start(ChatStartData)
 *     -> ( delta(ChatTokenDelta)
 *        | event:tool_call(ChatToolCall)
 *        | event:tool_result(ChatToolResult)
 *        | event:citation(ChatCitation) )*
 *     -> done(ChatDoneData) | error(Problem)
 *
 * Ordering: envelopes carry a monotonic `seq` per streamId. We ignore any
 * envelope whose seq we've already applied (duplicate / out-of-order on
 * reconnect) so a redelivered token never double-appends.
 */
import type {
  ChatCitation,
  ChatDoneData,
  ChatStartData,
  ChatToolCall,
  ChatToolResult,
  WsEnvelope,
  WsProblem,
} from '@/api';

export type StreamPhase = 'idle' | 'streaming' | 'done' | 'error';

/** The latest retrieval-tool activity, surfaced as "searching documents…". */
export interface ToolActivity {
  callId: string;
  tool: ChatToolCall['tool'];
  /** 'running' after tool_call; 'done' once the matching tool_result arrives. */
  status: 'running' | 'done';
  /** Hit count from the tool_result (absent while running). */
  hitCount?: number;
  summary?: string;
}

export interface StreamState {
  phase: StreamPhase;
  /** Accumulated assistant answer text (may be empty until the first delta). */
  text: string;
  /** Passage-level citations collected from event:citation (INV-3). */
  citations: ChatCitation[];
  /** Retrieval-tool activity, in arrival order (for "searching…" UX). */
  tools: ToolActivity[];
  /** start.data, once the stream opens. */
  start: ChatStartData | null;
  /** done.data, on terminal success. */
  done: ChatDoneData | null;
  /** The problem from a terminal WS error envelope (INV-* / disconnect). */
  problem: WsProblem | null;
  /** Highest applied seq, for dedupe / ordering. -1 before any envelope. */
  lastSeq: number;
}

export const initialStreamState: StreamState = {
  phase: 'idle',
  text: '',
  citations: [],
  tools: [],
  start: null,
  done: null,
  problem: null,
  lastSeq: -1,
};

/**
 * A synthetic terminal used by the hook when the socket drops without a `done`
 * or `error` envelope (unexpected disconnect → terminal with retry, AC-5).
 */
export const DISCONNECT_PROBLEM: WsProblem = {
  title: 'Connection lost',
  status: 0,
  detail: 'The answer stream disconnected before it finished.',
  code: 'stream_disconnected',
};

function asCitation(data: unknown): ChatCitation | null {
  if (typeof data !== 'object' || data === null) return null;
  const c = data as Record<string, unknown>;
  if (typeof c.id !== 'string') return null;
  if (typeof c.charStart !== 'number' || typeof c.charEnd !== 'number') return null;
  // A corpus-document citation carries a `documentId`; a web citation (#221)
  // carries a `url` instead (and no document_id — INV-3). Accept EITHER shape so
  // web citations survive the reducer; the renderer classifies by URL presence
  // (see model/citation.ts). Everything else stays required.
  const hasDoc = typeof c.documentId === 'string';
  const hasUrl = typeof c.url === 'string' && (c.url as string).trim().length > 0;
  if (!hasDoc && !hasUrl) return null;
  // Preserve any additive web fields (url/webTitle) verbatim — they ride through
  // the reducer to fromWsCitation. The cast is honest: the shape is a superset.
  return data as ChatCitation;
}

function asToolCall(data: unknown): ChatToolCall | null {
  if (typeof data !== 'object' || data === null) return null;
  const t = data as Record<string, unknown>;
  if (typeof t.callId !== 'string' || typeof t.tool !== 'string') return null;
  return data as ChatToolCall;
}

function asToolResult(data: unknown): ChatToolResult | null {
  if (typeof data !== 'object' || data === null) return null;
  const t = data as Record<string, unknown>;
  if (typeof t.callId !== 'string' || typeof t.hitCount !== 'number') return null;
  return data as ChatToolResult;
}

function deltaText(data: unknown): string | null {
  if (typeof data !== 'object' || data === null) return null;
  const d = data as Record<string, unknown>;
  return typeof d.text === 'string' ? d.text : null;
}

/** Fold one envelope into the stream state. Pure; returns a new state object. */
export function reduceStream(state: StreamState, envelope: WsEnvelope): StreamState {
  // Already terminal — ignore stragglers after done/error.
  if (state.phase === 'done' || state.phase === 'error') return state;
  // Dedupe / out-of-order guard: only apply strictly-newer envelopes.
  if (envelope.seq <= state.lastSeq) return state;

  const base = { ...state, lastSeq: envelope.seq };

  switch (envelope.type) {
    case 'start':
      return {
        ...base,
        phase: 'streaming',
        start: (envelope.data ?? null) as unknown as ChatStartData | null,
      };

    case 'delta': {
      const text = deltaText(envelope.data);
      if (text === null) return base;
      return { ...base, phase: 'streaming', text: base.text + text };
    }

    case 'event': {
      if (envelope.name === 'citation') {
        const citation = asCitation(envelope.data);
        if (!citation) return base;
        // Dedupe citations by id (a reconnect may resend).
        if (base.citations.some((c) => c.id === citation.id)) return base;
        return { ...base, citations: [...base.citations, citation] };
      }
      if (envelope.name === 'tool_call') {
        const call = asToolCall(envelope.data);
        if (!call) return base;
        const tools: ToolActivity[] = [
          ...base.tools,
          { callId: call.callId, tool: call.tool, status: 'running' },
        ];
        return { ...base, tools };
      }
      if (envelope.name === 'tool_result') {
        const result = asToolResult(envelope.data);
        if (!result) return base;
        const tools = base.tools.map((t) =>
          t.callId === result.callId
            ? {
                ...t,
                status: 'done' as const,
                hitCount: result.hitCount,
                ...(result.summary !== undefined ? { summary: result.summary } : {}),
              }
            : t,
        );
        return { ...base, tools };
      }
      // Unknown side-band event — ignore but keep the stream healthy.
      return base;
    }

    case 'done':
      return {
        ...base,
        phase: 'done',
        done: (envelope.data ?? null) as unknown as ChatDoneData | null,
      };

    case 'error':
      return { ...base, phase: 'error', problem: envelope.problem };

    default:
      return base;
  }
}

/** Apply a synthetic terminal (used on unexpected disconnect — AC-5). */
export function terminateWithDisconnect(state: StreamState): StreamState {
  if (state.phase === 'done' || state.phase === 'error') return state;
  return { ...state, phase: 'error', problem: DISCONNECT_PROBLEM };
}
