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
  CodeOutput,
  CodeResult,
  CodeRunStatus,
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
  /**
   * Whether the call succeeded (#377). Absent on the live stream (a relayed
   * tool_result is implicitly ok); false on a persisted governance denial or
   * tool failure — rendered as a danger badge.
   */
  ok?: boolean;
}

/**
 * A sandbox code run in flight on the chat stream (#232), assembled live from the
 * `code_output` chunks and finalized by the single `code_result`. The full record
 * (code, resource usage) is fetched after-the-fact from GET /code-runs/{runId} —
 * the stream carries only the id + streamed output + terminal outcome, so the
 * inspector shows something the instant a run starts, then backfills.
 */
export interface CodeRunActivity {
  /** The code_runs.id — the key to GET /code-runs/{id} for the full record. */
  runId: string;
  /** The originating run_python tool_call, when correlated. */
  callId?: string;
  /**
   * 'running' until the code_result arrives, then its terminal status. `queued` is
   * never seen on the stream (output only flows once running), so the live start
   * state is 'running'.
   */
  status: CodeRunStatus;
  /** stdout accumulated from code_output chunks (may be truncated at the cap, G7). */
  stdout: string;
  /** stderr accumulated from code_output chunks (may be truncated at the cap, G7). */
  stderr: string;
  /** Exit code from the code_result; null/absent until it finishes (or if killed). */
  exitCode?: number | null;
  /** Wall-clock duration (ms) from the code_result; absent until it finishes. */
  durationMs?: number | null;
  /** Artifact ids from the code_result; empty until (and unless) it emits files. */
  artifactIds: string[];
}

export interface StreamState {
  phase: StreamPhase;
  /** Accumulated assistant answer text (may be empty until the first delta). */
  text: string;
  /** Passage-level citations collected from event:citation (INV-3). */
  citations: ChatCitation[];
  /** Retrieval-tool activity, in arrival order (for "searching…" UX). */
  tools: ToolActivity[];
  /** Sandbox code runs on this stream, in first-seen order (the inspector, #232). */
  codeRuns: CodeRunActivity[];
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
  codeRuns: [],
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

function asCodeOutput(data: unknown): CodeOutput | null {
  if (typeof data !== 'object' || data === null) return null;
  const c = data as Record<string, unknown>;
  if (typeof c.runId !== 'string') return null;
  if (c.stream !== 'stdout' && c.stream !== 'stderr') return null;
  if (typeof c.text !== 'string') return null;
  return data as CodeOutput;
}

function asCodeResult(data: unknown): CodeResult | null {
  if (typeof data !== 'object' || data === null) return null;
  const c = data as Record<string, unknown>;
  if (typeof c.runId !== 'string' || typeof c.status !== 'string') return null;
  if (!Array.isArray(c.artifactIds)) return null;
  return data as CodeResult;
}

/**
 * Fold a code_output chunk into the code-run list — creating the run (as
 * 'running') on its first chunk, or appending to the matching stream buffer.
 */
function applyCodeOutput(runs: CodeRunActivity[], out: CodeOutput): CodeRunActivity[] {
  const existing = runs.find((r) => r.runId === out.runId);
  if (!existing) {
    return [
      ...runs,
      {
        runId: out.runId,
        ...(out.callId !== undefined ? { callId: out.callId } : {}),
        status: 'running',
        stdout: out.stream === 'stdout' ? out.text : '',
        stderr: out.stream === 'stderr' ? out.text : '',
        artifactIds: [],
      },
    ];
  }
  return runs.map((r) =>
    r.runId === out.runId
      ? {
          ...r,
          stdout: out.stream === 'stdout' ? r.stdout + out.text : r.stdout,
          stderr: out.stream === 'stderr' ? r.stderr + out.text : r.stderr,
        }
      : r,
  );
}

/**
 * Fold the single code_result into the code-run list — finalizing the run's
 * status/exit/duration/artifacts. A code_result with no preceding code_output
 * (e.g. a `denied` run that never ran) still creates the run so it's inspectable
 * (never a blank pane — AC-2).
 */
function applyCodeResult(runs: CodeRunActivity[], res: CodeResult): CodeRunActivity[] {
  const finalize = (r: CodeRunActivity): CodeRunActivity => ({
    ...r,
    status: res.status,
    exitCode: res.exitCode ?? null,
    durationMs: res.durationMs ?? null,
    artifactIds: res.artifactIds,
  });
  if (!runs.some((r) => r.runId === res.runId)) {
    return [
      ...runs,
      finalize({
        runId: res.runId,
        ...(res.callId !== undefined ? { callId: res.callId } : {}),
        status: res.status,
        stdout: '',
        stderr: '',
        artifactIds: [],
      }),
    ];
  }
  return runs.map((r) => (r.runId === res.runId ? finalize(r) : r));
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
        // The wire's ok/error are additive (absent ⇒ ok, CC-7). A denied/failed
        // call surfaces live as a danger badge (#377) — same failure text the
        // persisted trace renders, so live and reloaded turns never contradict.
        const failed = result.ok === false;
        const summary =
          result.summary ?? (failed ? `failed (${result.error ?? 'error'})` : undefined);
        const tools = base.tools.map((t) =>
          t.callId === result.callId
            ? {
                ...t,
                status: 'done' as const,
                hitCount: result.hitCount,
                ...(result.ok !== undefined ? { ok: result.ok } : {}),
                ...(summary !== undefined ? { summary } : {}),
              }
            : t,
        );
        return { ...base, tools };
      }
      if (envelope.name === 'code_output') {
        const out = asCodeOutput(envelope.data);
        if (!out) return base;
        return { ...base, codeRuns: applyCodeOutput(base.codeRuns, out) };
      }
      if (envelope.name === 'code_result') {
        const res = asCodeResult(envelope.data);
        if (!res) return base;
        return { ...base, codeRuns: applyCodeResult(base.codeRuns, res) };
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
