/**
 * Saved assistant test cases (#215) — the pure client-side store + verdict.
 * localStorage-backed, per assistant id; degrades to empty on read failure.
 */
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import type { AssistantTestTrace } from '@/api';
import {
  loadTestCases,
  newCaseId,
  removeTestCase,
  upsertTestCase,
  verdictFor,
  type AssistantTestCase,
} from './testCases';

function makeCase(overrides: Partial<AssistantTestCase> = {}): AssistantTestCase {
  return {
    id: newCaseId(),
    name: 'A case',
    input: 'hello',
    savedAt: Date.now(),
    ...overrides,
  };
}

function makeTrace(outputs: string): AssistantTestTrace {
  return {
    prompt: '',
    input: '',
    model: 'm',
    retrieval: [],
    toolCalls: [],
    outputs,
    errors: [],
    succeeded: true,
    durationMs: 1,
  };
}

beforeEach(() => window.localStorage.clear());
afterEach(() => window.localStorage.clear());

describe('testCases store', () => {
  it('is empty for an assistant with no saved cases', () => {
    expect(loadTestCases('a1')).toEqual([]);
  });

  it('upserts and reads back newest-first, scoped per assistant', () => {
    const older = makeCase({ name: 'old', savedAt: 1 });
    const newer = makeCase({ name: 'new', savedAt: 2 });
    upsertTestCase('a1', older);
    upsertTestCase('a1', newer);
    const list = loadTestCases('a1');
    expect(list.map((c) => c.name)).toEqual(['new', 'old']);
    // A different assistant id is a different bucket.
    expect(loadTestCases('a2')).toEqual([]);
  });

  it('upsert replaces a case by id', () => {
    const c = makeCase({ name: 'v1' });
    upsertTestCase('a1', c);
    upsertTestCase('a1', { ...c, name: 'v2' });
    const list = loadTestCases('a1');
    expect(list).toHaveLength(1);
    expect(list[0]?.name).toBe('v2');
  });

  it('removes a case by id', () => {
    const a = makeCase();
    const b = makeCase();
    upsertTestCase('a1', a);
    upsertTestCase('a1', b);
    const after = removeTestCase('a1', a.id);
    expect(after.map((c) => c.id)).toEqual([b.id]);
  });

  it('ignores malformed stored data (degrades to empty)', () => {
    window.localStorage.setItem('lumen.assistantTestCases.a1', 'not json');
    expect(loadTestCases('a1')).toEqual([]);
    window.localStorage.setItem('lumen.assistantTestCases.a1', JSON.stringify([{ bad: 1 }]));
    expect(loadTestCases('a1')).toEqual([]);
  });
});

describe('verdictFor', () => {
  it('is null when the case sets no expectation', () => {
    expect(verdictFor({ expected: undefined }, makeTrace('anything'))).toBeNull();
    expect(verdictFor({ expected: '   ' }, makeTrace('anything'))).toBeNull();
  });

  it('passes when the expected substring is present, fails otherwise', () => {
    expect(verdictFor({ expected: '14,600' }, makeTrace('It is $14,600.'))).toBe('pass');
    expect(verdictFor({ expected: '14,600' }, makeTrace('It is $12,000.'))).toBe('fail');
  });
});
