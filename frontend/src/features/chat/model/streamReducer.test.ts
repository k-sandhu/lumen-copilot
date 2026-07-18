/**
 * Stream reducer — the full chat answer-stream lifecycle folded into UI state.
 * Covers AC-1 (delta tokens, tool activity), AC-2 (citations), AC-5 (terminal
 * error + unexpected disconnect). Pure; no socket, no React.
 */
import { describe, it, expect } from 'vitest';
import {
  initialStreamState,
  reduceStream,
  terminateWithDisconnect,
  DISCONNECT_PROBLEM,
  type StreamState,
} from './streamReducer';
import type {
  DeltaEnvelope,
  DoneEnvelope,
  ErrorEnvelope,
  EventEnvelope,
  StartEnvelope,
} from '@/api';

const SID = 'stream-1';

function start(seq: number): StartEnvelope {
  return {
    type: 'start',
    streamId: SID,
    seq,
    data: { sessionId: 's', messageId: 'm', model: 'anthropic/claude-opus-4.8' },
  };
}
function delta(seq: number, text: string): DeltaEnvelope {
  return { type: 'delta', streamId: SID, seq, data: { text } };
}
function citation(seq: number, id: string): EventEnvelope {
  return {
    type: 'event',
    streamId: SID,
    seq,
    name: 'citation',
    data: {
      id,
      documentId: 'doc-1',
      documentName: 'Q4 plan.pdf',
      chunkId: 'chunk-1',
      snippet: 'cited passage',
      charStart: 10,
      charEnd: 40,
    },
  };
}
/**
 * A WEB citation (#221): carries a `url` and NO `documentId` (a web result has
 * no corpus document — INV-3). The reducer must accept it so it reaches the
 * renderer, which classifies web-vs-document by URL presence.
 */
function webCitation(seq: number, id: string, url: string): EventEnvelope {
  return {
    type: 'event',
    streamId: SID,
    seq,
    name: 'citation',
    data: {
      id,
      documentName: 'A web page',
      snippet: 'a web snippet',
      charStart: 0,
      charEnd: 13,
      url,
    },
  };
}
function toolCall(seq: number, callId: string): EventEnvelope {
  return {
    type: 'event',
    streamId: SID,
    seq,
    name: 'tool_call',
    data: { callId, tool: 'search_text', args: { q: 'budget' } },
  };
}
function toolResult(seq: number, callId: string, hitCount: number): EventEnvelope {
  return {
    type: 'event',
    streamId: SID,
    seq,
    name: 'tool_result',
    data: { callId, tool: 'search_text', hitCount, summary: 'found passages' },
  };
}
function codeOutput(
  seq: number,
  runId: string,
  stream: 'stdout' | 'stderr',
  text: string,
): EventEnvelope {
  return {
    type: 'event',
    streamId: SID,
    seq,
    name: 'code_output',
    data: { runId, callId: 'call-1', stream, text },
  };
}
function codeResult(
  seq: number,
  runId: string,
  status: 'succeeded' | 'failed' | 'timeout' | 'killed' | 'denied',
  over: { exitCode?: number | null; durationMs?: number | null; artifactIds?: string[] } = {},
): EventEnvelope {
  return {
    type: 'event',
    streamId: SID,
    seq,
    name: 'code_result',
    data: {
      runId,
      callId: 'call-1',
      status,
      exitCode: over.exitCode ?? null,
      durationMs: over.durationMs ?? null,
      artifactIds: over.artifactIds ?? [],
    },
  };
}
function done(seq: number, citationCount: number): DoneEnvelope {
  return {
    type: 'done',
    streamId: SID,
    seq,
    data: { messageId: 'm', finishReason: 'stop', citationCount },
  };
}
function errorEnv(seq: number): ErrorEnvelope {
  return {
    type: 'error',
    streamId: SID,
    seq,
    problem: {
      title: 'Upstream error',
      status: 502,
      detail: 'model unavailable',
      code: 'upstream',
    },
  };
}

function fold(state: StreamState, ...envs: Parameters<typeof reduceStream>[1][]): StreamState {
  return envs.reduce(reduceStream, state);
}

describe('reduceStream', () => {
  it('streams delta tokens in order into accumulated text (AC-1)', () => {
    const s = fold(initialStreamState, start(0), delta(1, 'Hello'), delta(2, ', world'));
    expect(s.phase).toBe('streaming');
    expect(s.text).toBe('Hello, world');
    expect(s.start?.model).toBe('anthropic/claude-opus-4.8');
  });

  it('ignores duplicate / out-of-order seq (no double-append on reconnect)', () => {
    const s = fold(initialStreamState, start(0), delta(1, 'A'), delta(1, 'A'), delta(0, 'X'));
    expect(s.text).toBe('A');
  });

  it('surfaces tool activity: running on tool_call, done on tool_result (AC-1)', () => {
    let s = fold(initialStreamState, start(0), toolCall(1, 'c1'));
    expect(s.tools).toHaveLength(1);
    expect(s.tools[0]).toMatchObject({ callId: 'c1', tool: 'search_text', status: 'running' });

    s = reduceStream(s, toolResult(2, 'c1', 3));
    expect(s.tools[0]).toMatchObject({ status: 'done', hitCount: 3, summary: 'found passages' });
    // The wire's additive ok is absent on this result — the activity leaves it
    // unset (treated as ok), matching CC-7 back-compat.
    expect(s.tools[0]?.ok).toBeUndefined();
  });

  it('propagates a denied/failed tool_result (ok=false) with a failure summary (#377)', () => {
    let s = fold(initialStreamState, start(0), toolCall(1, 'c1'));
    s = reduceStream(s, {
      type: 'event',
      streamId: SID,
      seq: 2,
      name: 'tool_result',
      data: { callId: 'c1', tool: 'search_text', hitCount: 0, ok: false, error: 'tool_denied' },
    } as EventEnvelope);
    // Live failures must not render as successes (they'd contradict the
    // persisted trace after reload): ok=false flows through, and the summary
    // matches the persisted-trace failure text exactly.
    expect(s.tools[0]).toMatchObject({
      status: 'done',
      ok: false,
      summary: 'failed (tool_denied)',
    });
  });

  it('collects citations and dedupes by id (AC-2)', () => {
    const s = fold(
      initialStreamState,
      start(0),
      delta(1, 'answer'),
      citation(2, 'cite-1'),
      citation(3, 'cite-2'),
      citation(4, 'cite-1'), // duplicate id
    );
    expect(s.citations.map((c) => c.id)).toEqual(['cite-1', 'cite-2']);
    expect(s.citations[0]).toMatchObject({ documentId: 'doc-1', charStart: 10, charEnd: 40 });
  });

  it('accepts a web citation (url, no documentId) so it reaches the renderer (#221)', () => {
    const s = fold(
      initialStreamState,
      start(0),
      delta(1, 'answer'),
      webCitation(2, 'web-1', 'https://example.com/a'),
    );
    expect(s.citations).toHaveLength(1);
    expect(s.citations[0]).toMatchObject({ id: 'web-1', url: 'https://example.com/a' });
  });

  it('still rejects a malformed citation with neither documentId nor url (#221)', () => {
    const bad: EventEnvelope = {
      type: 'event',
      streamId: SID,
      seq: 2,
      name: 'citation',
      data: { id: 'x', snippet: 's', charStart: 0, charEnd: 1 },
    };
    const s = fold(initialStreamState, start(0), bad);
    expect(s.citations).toHaveLength(0);
  });

  it('reaches done with the terminal summary (AC-2 persist trigger)', () => {
    const s = fold(initialStreamState, start(0), delta(1, 'hi'), citation(2, 'c'), done(3, 1));
    expect(s.phase).toBe('done');
    expect(s.done).toMatchObject({ messageId: 'm', finishReason: 'stop', citationCount: 1 });
  });

  it('supports a zero-citation answer honestly (AC-5: no fabricated refs)', () => {
    const s = fold(initialStreamState, start(0), delta(1, 'I could not find that.'), done(2, 0));
    expect(s.phase).toBe('done');
    expect(s.citations).toHaveLength(0);
    expect(s.done?.citationCount).toBe(0);
  });

  it('handles a terminal WS error envelope (AC-5)', () => {
    const s = fold(initialStreamState, start(0), delta(1, 'partial'), errorEnv(2));
    expect(s.phase).toBe('error');
    expect(s.problem).toMatchObject({ status: 502, code: 'upstream' });
    // The partial text is preserved so the user still sees what arrived.
    expect(s.text).toBe('partial');
  });

  it('ignores envelopes after a terminal state', () => {
    const after = fold(initialStreamState, start(0), done(1, 0), delta(2, 'late'));
    expect(after.phase).toBe('done');
    expect(after.text).toBe('');
  });

  it('terminateWithDisconnect marks a dropped stream as error (AC-5)', () => {
    const streaming = fold(initialStreamState, start(0), delta(1, 'half'));
    const dropped = terminateWithDisconnect(streaming);
    expect(dropped.phase).toBe('error');
    expect(dropped.problem).toEqual(DISCONNECT_PROBLEM);
    expect(dropped.text).toBe('half');
  });

  it('does not override an existing terminal on disconnect', () => {
    const finished = fold(initialStreamState, start(0), done(1, 0));
    expect(terminateWithDisconnect(finished)).toBe(finished);
  });

  // --- code runs (#232) ------------------------------------------------------

  it('assembles a code run: creates it running on the first code_output chunk (AC-1)', () => {
    const s = fold(initialStreamState, start(0), codeOutput(1, 'run-1', 'stdout', 'Hello'));
    expect(s.codeRuns).toHaveLength(1);
    expect(s.codeRuns[0]).toMatchObject({
      runId: 'run-1',
      status: 'running',
      stdout: 'Hello',
      stderr: '',
    });
  });

  it('appends streamed stdout/stderr chunks in order onto the matching run (AC-1)', () => {
    const s = fold(
      initialStreamState,
      start(0),
      codeOutput(1, 'run-1', 'stdout', 'line 1\n'),
      codeOutput(2, 'run-1', 'stderr', 'warn: x\n'),
      codeOutput(3, 'run-1', 'stdout', 'line 2\n'),
    );
    expect(s.codeRuns[0]?.stdout).toBe('line 1\nline 2\n');
    expect(s.codeRuns[0]?.stderr).toBe('warn: x\n');
  });

  it('finalizes the run on code_result with status/exit/duration/artifacts (AC-1)', () => {
    const s = fold(
      initialStreamState,
      start(0),
      codeOutput(1, 'run-1', 'stdout', 'done'),
      codeResult(2, 'run-1', 'succeeded', {
        exitCode: 0,
        durationMs: 820,
        artifactIds: ['art-1', 'art-2'],
      }),
    );
    expect(s.codeRuns[0]).toMatchObject({
      status: 'succeeded',
      exitCode: 0,
      durationMs: 820,
      artifactIds: ['art-1', 'art-2'],
      stdout: 'done',
    });
  });

  it('creates an inspectable run from a lone code_result with no output — e.g. denied (AC-2)', () => {
    // A denied run never executes, so no code_output precedes its result. The
    // reducer still records it so the inspector is never blank.
    const s = fold(initialStreamState, start(0), codeResult(1, 'run-x', 'denied'));
    expect(s.codeRuns).toHaveLength(1);
    expect(s.codeRuns[0]).toMatchObject({
      runId: 'run-x',
      status: 'denied',
      stdout: '',
      stderr: '',
    });
  });

  it('tracks two concurrent runs independently by runId', () => {
    const s = fold(
      initialStreamState,
      start(0),
      codeOutput(1, 'run-1', 'stdout', 'a'),
      codeOutput(2, 'run-2', 'stdout', 'b'),
      codeResult(3, 'run-1', 'succeeded', { exitCode: 0 }),
    );
    expect(s.codeRuns).toHaveLength(2);
    expect(s.codeRuns.find((r) => r.runId === 'run-1')?.status).toBe('succeeded');
    expect(s.codeRuns.find((r) => r.runId === 'run-2')?.status).toBe('running');
  });

  it('ignores a malformed code_output (bad stream / missing text) without corrupting state', () => {
    const bad: EventEnvelope = {
      type: 'event',
      streamId: SID,
      seq: 1,
      name: 'code_output',
      data: { runId: 'run-1', stream: 'stdxxx', text: 'x' },
    };
    const s = fold(initialStreamState, start(0), bad);
    expect(s.codeRuns).toHaveLength(0);
  });

  it('code_result is a run-terminal, not a stream-terminal — the stream still streams', () => {
    const s = fold(
      initialStreamState,
      start(0),
      codeOutput(1, 'run-1', 'stdout', 'x'),
      codeResult(2, 'run-1', 'succeeded', { exitCode: 0 }),
      delta(3, ' and the answer continues'),
    );
    expect(s.phase).toBe('streaming');
    expect(s.text).toBe(' and the answer continues');
    expect(s.codeRuns[0]?.status).toBe('succeeded');
  });
});

// --- Spec 0006 (#429): step / ask_user / suggestions events ------------------

function stepEvent(
  seq: number,
  key: string,
  state: 'started' | 'completed',
  extra: Record<string, unknown> = {},
): EventEnvelope {
  return {
    type: 'event',
    streamId: SID,
    seq,
    name: 'step',
    data: { key, label: key === 'think' ? 'Thinking' : 'Preparing', state, ...extra },
  };
}

describe('spec 0006 events (#429)', () => {
  it('upserts steps by key in first-seen order (AC-1)', () => {
    let s: StreamState = reduceStream(initialStreamState, start(0));
    s = reduceStream(s, stepEvent(1, 'prepare', 'started'));
    s = reduceStream(s, stepEvent(2, 'prepare', 'completed'));
    s = reduceStream(s, stepEvent(3, 'think', 'started', { turn: 1 }));
    s = reduceStream(
      s,
      stepEvent(4, 'think', 'completed', { turn: 1, detail: 'requested 1 tool' }),
    );
    s = reduceStream(s, stepEvent(5, 'think', 'started', { turn: 2 }));
    expect(s.steps.map((x) => [x.key, x.state])).toEqual([
      ['prepare', 'completed'],
      ['think', 'started'],
    ]);
    expect(s.steps[1]?.turn).toBe(2);
    // Malformed step payloads are dropped, stream stays healthy.
    const bad = reduceStream(s, {
      type: 'event',
      streamId: SID,
      seq: 6,
      name: 'step',
      data: { key: 'x' },
    });
    expect(bad.steps).toEqual(s.steps);
  });

  it('folds ask_user and replays idempotently by seq (AC-2/AC-N3)', () => {
    let s: StreamState = reduceStream(initialStreamState, start(0));
    const ask: EventEnvelope = {
      type: 'event',
      streamId: SID,
      seq: 1,
      name: 'ask_user',
      data: {
        messageId: 'm',
        question: 'Which quarter?',
        options: [{ label: 'Q1' }, { label: 'Q2', description: 'Apr-Jun' }],
        allowFreeText: true,
      },
    };
    s = reduceStream(s, ask);
    expect(s.askUser?.question).toBe('Which quarter?');
    expect(s.askUser?.options).toHaveLength(2);
    // A replayed (duplicate-seq) envelope is a no-op — options never re-arm twice.
    const replayed = reduceStream(s, ask);
    expect(replayed).toBe(s);
    // Malformed (no options) is dropped.
    const bad = reduceStream(s, {
      type: 'event',
      streamId: SID,
      seq: 2,
      name: 'ask_user',
      data: { messageId: 'm', question: 'x', options: [] },
    });
    expect(bad.askUser).toEqual(s.askUser);
  });

  it('folds suggestions, filtering non-string/empty items (AC-3)', () => {
    let s: StreamState = reduceStream(initialStreamState, start(0));
    s = reduceStream(s, delta(1, 'Answer.'));
    s = reduceStream(s, {
      type: 'event',
      streamId: SID,
      seq: 2,
      name: 'suggestions',
      data: { messageId: 'm', suggestions: ['What next?', '', 42, 'Who owns it?'] },
    });
    expect(s.suggestions).toEqual({ messageId: 'm', suggestions: ['What next?', 'Who owns it?'] });
    // All-empty payload is dropped entirely.
    const bad = reduceStream(s, {
      type: 'event',
      streamId: SID,
      seq: 3,
      name: 'suggestions',
      data: { messageId: 'm', suggestions: [''] },
    });
    expect(bad.suggestions).toEqual(s.suggestions);
  });
});

describe('narration (ADR-0016 §6, #414)', () => {
  function narration(seq: number, text: unknown, turn?: unknown): EventEnvelope {
    return {
      type: 'event',
      streamId: SID,
      seq,
      name: 'narration',
      data: { text, turn },
    } as EventEnvelope;
  }

  it('accumulates within a turn, resets on a new turn, never touches text', () => {
    let s = initialStreamState;
    s = reduceStream(s, narration(1, 'Searching ', 1));
    s = reduceStream(s, narration(2, 'the docs…', 1));
    expect(s.narration).toEqual({ turn: 1, text: 'Searching the docs…' });
    expect(s.text).toBe('');
    s = reduceStream(s, narration(3, 'Refining.', 2));
    expect(s.narration).toEqual({ turn: 2, text: 'Refining.' });
  });

  it('ignores malformed narration payloads', () => {
    let s = initialStreamState;
    s = reduceStream(s, narration(1, 42, undefined));
    expect(s.narration).toBeNull();
  });
});
