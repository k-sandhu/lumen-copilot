/**
 * Saved assistant test cases (E6-5, #215) — persisted CLIENT-SIDE (localStorage),
 * scoped per assistant id. The backend intentionally ships no `assistant_test_cases`
 * table in this slice (the scope fence keeps automated eval scoring / regression
 * gating OUT); a saved case is a builder convenience — a named sample input you can
 * re-run on demand and, if you set an `expected` substring, see pass/fail against.
 *
 * Pure module: read/write + a tiny pass/fail check, no React and no transport. The
 * store is best-effort — a private-mode / quota / disabled-storage failure degrades
 * to "no saved cases" rather than throwing (the panel still runs ad-hoc tests).
 */
import type { AssistantTestTrace } from '@/api';

/** A saved, re-runnable test case for one assistant (client-side only). */
export interface AssistantTestCase {
  /** Stable local id (not a server row). */
  id: string;
  /** A short human name for the case. */
  name: string;
  /** The sample input the case runs. */
  input: string;
  /**
   * Optional expected-substring assertion: if set, a re-run passes when the trace's
   * `outputs` contains it (a simple, honest regression check — not a full eval).
   */
  expected?: string;
  /** When the case was saved (epoch ms), newest first in the list. */
  savedAt: number;
}

const KEY_PREFIX = 'lumen.assistantTestCases.';

function keyFor(assistantId: string): string {
  return `${KEY_PREFIX}${assistantId}`;
}

/** The saved cases for an assistant (newest first). Empty on any read failure. */
export function loadTestCases(assistantId: string): AssistantTestCase[] {
  try {
    const raw = window.localStorage.getItem(keyFor(assistantId));
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed
      .filter(isTestCase)
      .sort((a, b) => b.savedAt - a.savedAt);
  } catch {
    return [];
  }
}

/** Persist the full list for an assistant (best-effort). Returns success. */
export function saveTestCases(assistantId: string, cases: AssistantTestCase[]): boolean {
  try {
    window.localStorage.setItem(keyFor(assistantId), JSON.stringify(cases));
    return true;
  } catch {
    return false;
  }
}

/** Add (or update, by id) a case and persist. Returns the new list (newest first). */
export function upsertTestCase(
  assistantId: string,
  next: AssistantTestCase,
): AssistantTestCase[] {
  const existing = loadTestCases(assistantId).filter((c) => c.id !== next.id);
  const list = [next, ...existing].sort((a, b) => b.savedAt - a.savedAt);
  saveTestCases(assistantId, list);
  return list;
}

/** Remove a case by id and persist. Returns the new list. */
export function removeTestCase(assistantId: string, caseId: string): AssistantTestCase[] {
  const list = loadTestCases(assistantId).filter((c) => c.id !== caseId);
  saveTestCases(assistantId, list);
  return list;
}

/**
 * The regression verdict for a re-run: `pass` / `fail` when the case set an
 * `expected` substring, else `null` (no assertion — the run just produced a trace).
 */
export function verdictFor(
  testCase: Pick<AssistantTestCase, 'expected'>,
  trace: AssistantTestTrace,
): 'pass' | 'fail' | null {
  const expected = testCase.expected?.trim();
  if (!expected) return null;
  return trace.outputs.includes(expected) ? 'pass' : 'fail';
}

/** A short, collision-resistant local id for a saved case. */
export function newCaseId(): string {
  return `tc_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`;
}

function isTestCase(value: unknown): value is AssistantTestCase {
  if (typeof value !== 'object' || value === null) return false;
  const v = value as Record<string, unknown>;
  return (
    typeof v.id === 'string' &&
    typeof v.name === 'string' &&
    typeof v.input === 'string' &&
    typeof v.savedAt === 'number' &&
    (v.expected === undefined || typeof v.expected === 'string')
  );
}
