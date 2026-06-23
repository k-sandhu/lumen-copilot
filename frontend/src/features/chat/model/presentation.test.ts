/**
 * Pure chat presentation helpers (#89) — the trust-signal derivation that drives
 * the re-skin. Asserts the retrieval-trace summary/steps, freshness/staleness,
 * the model-badge label, and the SourceInspector passage, including the edge
 * cases (zero citations, no tools, missing timestamps).
 */
import { describe, it, expect } from 'vitest';
import {
  relativeTime,
  isStale,
  modelBadgeLabel,
  buildRetrievalSummary,
  passageFromCitation,
  STALE_AFTER_MS,
} from './presentation';
import type { UiCitation } from './citation';
import type { ToolActivity } from './streamReducer';

const NOW = Date.parse('2026-06-19T12:00:00Z');

function cite(over: Partial<UiCitation> = {}): UiCitation {
  return {
    id: 'c1',
    documentId: 'doc-1',
    documentName: 'Q3 Pricing.pdf',
    chunkId: 'k1',
    snippet: 'A 7% list-price increase applies to the Platform tier.',
    charStart: 0,
    charEnd: 40,
    ...over,
  };
}

describe('relativeTime', () => {
  it('returns null for absent / unparseable timestamps (pill is omitted)', () => {
    expect(relativeTime(undefined, NOW)).toBeNull();
    expect(relativeTime('not-a-date', NOW)).toBeNull();
  });

  it('formats recent → distant spans', () => {
    expect(relativeTime('2026-06-19T11:59:30Z', NOW)).toBe('Just now');
    expect(relativeTime('2026-06-19T11:30:00Z', NOW)).toBe('30m ago');
    expect(relativeTime('2026-06-19T10:00:00Z', NOW)).toBe('2h ago');
    expect(relativeTime('2026-06-18T12:00:00Z', NOW)).toBe('Yesterday');
    expect(relativeTime('2026-06-16T12:00:00Z', NOW)).toBe('3d ago');
    expect(relativeTime('2026-06-05T12:00:00Z', NOW)).toBe('2w ago');
  });
});

describe('isStale', () => {
  it('is false for fresh / missing timestamps and true past the window', () => {
    expect(isStale(undefined, NOW)).toBe(false);
    expect(isStale('2026-06-18T12:00:00Z', NOW)).toBe(false);
    const old = new Date(NOW - STALE_AFTER_MS - 1000).toISOString();
    expect(isStale(old, NOW)).toBe(true);
  });
});

describe('modelBadgeLabel', () => {
  it('prefers the friendly label, then the trailing id segment, else null', () => {
    expect(modelBadgeLabel('anthropic/claude-opus-4.8', 'Claude Opus 4.8')).toBe('Claude Opus 4.8');
    expect(modelBadgeLabel('anthropic/claude-opus-4.8')).toBe('claude-opus-4.8');
    expect(modelBadgeLabel('gpt-4o')).toBe('gpt-4o');
    expect(modelBadgeLabel(undefined)).toBeNull();
  });
});

describe('buildRetrievalSummary', () => {
  const doneTool = (over: Partial<ToolActivity> = {}): ToolActivity => ({
    callId: 't1',
    tool: 'search_text',
    status: 'done',
    hitCount: 412,
    ...over,
  });

  it('counts distinct sources and summed passages', () => {
    const result = buildRetrievalSummary(
      [cite({ documentId: 'doc-1' }), cite({ id: 'c2', documentId: 'doc-2' }), cite({ id: 'c3', documentId: 'doc-1' })],
      [doneTool({ hitCount: 800 }), doneTool({ callId: 't2', hitCount: 404 })],
    );
    expect(result.summary).toBe('Looked at 2 sources · 1,204 passages');
    expect(result.hasContent).toBe(true);
  });

  it('surfaces excluded candidates as a muted step + in the summary (filter #4)', () => {
    const result = buildRetrievalSummary([cite()], [doneTool({ hitCount: 1204 })], 38);
    expect(result.summary).toBe('Looked at 1 source · 1,204 passages · 38 excluded');
    const excluded = result.steps.find((s) => s.excluded);
    expect(excluded?.label).toMatch(/38 results excluded/i);
  });

  it('ignores still-running tools when building steps but still has source content', () => {
    const result = buildRetrievalSummary(
      [cite()],
      [{ callId: 't1', tool: 'search_text', status: 'running' }],
    );
    // No completed tool → no "Searched sources" step, but the cited source counts.
    expect(result.steps.some((s) => /searched sources/i.test(s.label))).toBe(false);
    expect(result.steps.some((s) => /cited 1 source/i.test(s.label))).toBe(true);
    expect(result.hasContent).toBe(true);
  });

  it('reports no content for a zero-citation, zero-tool turn', () => {
    const result = buildRetrievalSummary([], []);
    expect(result.summary).toBe('Looked at 0 sources');
    expect(result.hasContent).toBe(false);
  });
});

describe('passageFromCitation', () => {
  it('highlights the whole cited snippet (it is what was cited)', () => {
    const passage = passageFromCitation(cite({ snippet: '  cited text  ' }));
    expect(passage.runs).toEqual([{ text: 'cited text', highlight: true }]);
  });

  it('yields no runs for an empty snippet', () => {
    expect(passageFromCitation(cite({ snippet: '   ' })).runs).toEqual([]);
  });
});
