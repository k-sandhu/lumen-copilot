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
    problem: { title: 'Upstream error', status: 502, detail: 'model unavailable', code: 'upstream' },
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
});
