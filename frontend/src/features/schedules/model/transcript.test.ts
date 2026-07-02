/**
 * Unit tests for the run transcript + citation mappers (#237, AC-2). Covers
 * assembling the answer from delta steps, folding steps into display items,
 * citation → passage, and the trace summary.
 */
import { describe, it, expect } from 'vitest';
import type { Citation, RunStep } from '@/api';
import {
  assembleAnswer,
  citationToPassage,
  toTranscript,
  traceSummary,
} from './transcript';

const STEPS: RunStep[] = [
  { seq: 0, kind: 'delta', payload: { text: 'Hello ' }, created_at: 't' },
  { seq: 2, kind: 'delta', payload: { text: 'world' }, created_at: 't' },
  { seq: 1, kind: 'tool_call', payload: { name: 'search', args: { q: 'x' } }, created_at: 't' },
  { seq: 3, kind: 'tool_result', payload: { name: 'search', hits: 3 }, created_at: 't' },
  { seq: 4, kind: 'citation', payload: { snippet: 'a cited passage' }, created_at: 't' },
];

describe('assembleAnswer', () => {
  it('concatenates delta text in seq order', () => {
    expect(assembleAnswer(STEPS)).toBe('Hello world');
  });
  it('returns empty for no steps', () => {
    expect(assembleAnswer(undefined)).toBe('');
  });
});

describe('toTranscript', () => {
  it('folds non-delta steps into labelled items in seq order', () => {
    const items = toTranscript(STEPS);
    expect(items.map((i) => i.kind)).toEqual(['tool_call', 'tool_result', 'citation']);
    expect(items[0]?.label).toMatch(/Called search/);
    expect(items[1]?.label).toMatch(/Result from search/);
    expect(items[2]?.label).toMatch(/Passage cited/);
  });
  it('labels an error step', () => {
    const items = toTranscript([{ seq: 0, kind: 'error', payload: { code: 'boom' }, created_at: 't' }]);
    expect(items[0]?.label).toBe('Error');
    expect(items[0]?.detail).toMatch(/boom/);
  });
});

describe('citationToPassage', () => {
  it('highlights the whole snippet run', () => {
    const citation: Citation = {
      id: 'c1',
      document_id: 'd1',
      document_name: 'Q3.pdf',
      chunk_id: 'k1',
      snippet: 'the cited text',
      char_start: 0,
      char_end: 14,
    };
    expect(citationToPassage(citation)).toEqual({ runs: [{ text: 'the cited text', highlight: true }] });
  });
});

describe('traceSummary', () => {
  it('counts tool calls and citations', () => {
    const citations = [{ id: 'c1' }] as unknown as Citation[];
    expect(traceSummary(STEPS, citations)).toBe('1 tool call · 1 citation');
  });
  it('pluralizes correctly with zero', () => {
    expect(traceSummary([], [])).toBe('0 tool calls · 0 citations');
  });
});
