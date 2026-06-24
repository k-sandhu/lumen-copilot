/**
 * Pure presentation helpers for the search slice (#84). These are total
 * functions — every branch (incl. the invariant: spans tile the snippet exactly
 * once) is asserted here, independent of React.
 */
import { describe, it, expect } from 'vitest';
import type { MatchSpan, ResultSource, SearchResult } from '@/api';
import {
  freshnessLabel,
  hasInlineCitations,
  isStale,
  permissionLabel,
  segmentAnswer,
  sourceFacets,
  sourceGlyph,
  sourceLabel,
  STALE_AFTER_MS,
  toPassageRuns,
  toPermissionLevel,
  trimNotice,
  typeFacets,
} from './presentation';

function res(source: ResultSource, type: string): SearchResult {
  return {
    id: `${source}-${type}-${Math.random()}`,
    title: 't',
    snippet: 's',
    match_spans: [],
    why_matched: 'w',
    source,
    type,
    last_indexed: '2026-06-01T00:00:00Z',
    permission: 'allowed',
  };
}

describe('toPermissionLevel / permissionLabel', () => {
  it('maps allowed → granted and restricted → restricted', () => {
    expect(toPermissionLevel('allowed')).toBe('granted');
    expect(toPermissionLevel('restricted')).toBe('restricted');
  });
  it('labels each permission distinctly', () => {
    expect(permissionLabel('allowed')).toMatch(/access/i);
    expect(permissionLabel('restricted')).toMatch(/withheld/i);
  });
});

describe('toPassageRuns', () => {
  it('splits a snippet into highlighted and plain runs', () => {
    const runs = toPassageRuns('hello world', [{ start: 0, end: 5 }]).runs;
    expect(runs).toEqual([{ text: 'hello', highlight: true }, { text: ' world' }]);
  });

  it('returns the whole snippet as one plain run when there are no spans', () => {
    expect(toPassageRuns('plain text', []).runs).toEqual([{ text: 'plain text' }]);
  });

  it('returns no runs for an empty snippet', () => {
    expect(toPassageRuns('', []).runs).toEqual([]);
  });

  it('INVARIANT: runs concatenate back to the original snippet exactly once', () => {
    const snippet = 'the quick brown fox jumps';
    const spans: MatchSpan[] = [
      { start: 16, end: 19 }, // fox (out of order)
      { start: 4, end: 9 }, // quick
    ];
    const runs = toPassageRuns(snippet, spans).runs;
    expect(runs.map((r) => r.text).join('')).toBe(snippet);
    // the highlighted runs are exactly the matched substrings
    expect(runs.filter((r) => r.highlight).map((r) => r.text)).toEqual(['quick', 'fox']);
  });

  it('merges overlapping / adjacent spans so no character is double-counted', () => {
    const snippet = 'abcdef';
    const runs = toPassageRuns(snippet, [
      { start: 0, end: 3 },
      { start: 2, end: 5 }, // overlaps the first
    ]).runs;
    expect(runs.map((r) => r.text).join('')).toBe(snippet);
    expect(runs.filter((r) => r.highlight)).toEqual([{ text: 'abcde', highlight: true }]);
  });

  it('clamps out-of-bounds spans to the snippet length', () => {
    const runs = toPassageRuns('abc', [{ start: 1, end: 99 }]).runs;
    expect(runs).toEqual([{ text: 'a' }, { text: 'bc', highlight: true }]);
  });
});

describe('freshnessLabel', () => {
  const now = Date.parse('2026-06-19T12:00:00Z');
  it('"just now" under a minute', () => {
    expect(freshnessLabel('2026-06-19T11:59:30Z', now)).toBe('Indexed just now');
  });
  it('singular vs plural hours', () => {
    expect(freshnessLabel('2026-06-19T11:00:00Z', now)).toBe('Indexed an hour ago');
    expect(freshnessLabel('2026-06-19T09:00:00Z', now)).toBe('Indexed 3 hours ago');
  });
  it('days and weeks', () => {
    expect(freshnessLabel('2026-06-17T12:00:00Z', now)).toBe('Indexed 2 days ago');
    expect(freshnessLabel('2026-06-05T12:00:00Z', now)).toBe('Indexed 2 weeks ago');
  });
  it('degrades to "recently" on an unparseable timestamp (never throws)', () => {
    expect(freshnessLabel('not-a-date', now)).toBe('Indexed recently');
  });
});

describe('isStale', () => {
  const now = Date.parse('2026-06-19T12:00:00Z');
  it('is false within the freshness window', () => {
    expect(isStale('2026-06-01T12:00:00Z', now)).toBe(false);
  });
  it('is true past the window', () => {
    expect(isStale(new Date(now - STALE_AFTER_MS - 1).toISOString(), now)).toBe(true);
  });
  it('is false for an unparseable timestamp', () => {
    expect(isStale('nope', now)).toBe(false);
  });
});

describe('sourceGlyph', () => {
  it('has a distinct glyph per source kind', () => {
    expect(sourceGlyph('upload')).not.toBe(sourceGlyph('chat'));
    expect(sourceGlyph('connector')).toBeTruthy();
  });
});

describe('trimNotice', () => {
  it('is null when nothing is hidden', () => {
    expect(trimNotice(0)).toBeNull();
    expect(trimNotice(-1)).toBeNull();
  });
  it('discloses the hidden count without leaking content (spec 0004 INV-2)', () => {
    expect(trimNotice(1)).toBe("1 result hidden — you don't have access");
    expect(trimNotice(3)).toBe("3 results hidden — you don't have access");
  });
});

describe('sourceLabel', () => {
  it('labels each REAL source kind from the frozen enum (no invented connectors)', () => {
    expect(sourceLabel('upload')).toMatch(/uploaded/i);
    expect(sourceLabel('chat')).toMatch(/chat/i);
    expect(sourceLabel('connector')).toMatch(/connected/i);
  });
});

describe('sourceFacets', () => {
  it('tallies only the source kinds present in the data, in enum order', () => {
    const facets = sourceFacets([
      res('chat', 'message'),
      res('upload', 'document'),
      res('upload', 'document'),
    ]);
    // upload before chat (enum order), counts honest, connector absent (no data).
    expect(facets).toEqual([
      { source: 'upload', count: 2 },
      { source: 'chat', count: 1 },
    ]);
  });
  it('is empty for no results (never a faked connector row)', () => {
    expect(sourceFacets([])).toEqual([]);
  });
});

describe('typeFacets', () => {
  it('derives content-type facets from the data, most-frequent first', () => {
    const facets = typeFacets([
      res('upload', 'document'),
      res('chat', 'message'),
      res('upload', 'document'),
    ]);
    expect(facets).toEqual([
      { type: 'document', count: 2 },
      { type: 'message', count: 1 },
    ]);
  });
});

describe('segmentAnswer / hasInlineCitations', () => {
  it('splits text into plain runs and resolvable [n] markers', () => {
    expect(segmentAnswer('A [1] B [2].', 2)).toEqual([
      { text: 'A ' },
      { text: '[1]', cite: 1 },
      { text: ' B ' },
      { text: '[2]', cite: 2 },
      { text: '.' },
    ]);
  });
  it('leaves an out-of-range [n] as plain text (never an un-resolvable citation)', () => {
    // [9] points past the 1 available citation → stays literal.
    const segs = segmentAnswer('one [1] nine [9]', 1);
    expect(segs.some((s) => s.cite === 1)).toBe(true);
    expect(segs.some((s) => s.cite === 9)).toBe(false);
    expect(segs.map((s) => s.text).join('')).toBe('one [1] nine [9]');
  });
  it('reports whether any resolvable inline marker is present', () => {
    expect(hasInlineCitations('answer [1]', 1)).toBe(true);
    expect(hasInlineCitations('answer with no markers', 1)).toBe(false);
    expect(hasInlineCitations('answer [3]', 1)).toBe(false);
  });
});
