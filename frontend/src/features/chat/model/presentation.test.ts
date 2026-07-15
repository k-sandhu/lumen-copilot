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
  groupCitationsByDocument,
  passageFromCitation,
  sourceMetadataRows,
  METADATA_UNKNOWN,
  STALE_AFTER_MS,
  dayBucket,
  groupSessionsByDay,
  sessionMeta,
  initialModes,
  modeAvailability,
  usedWebSearch,
  partitionCitations,
  DEFAULT_CHAT_MODES,
} from './presentation';
import { toolActivityFromInvocations } from './presentation';
import type { UiCitation } from './citation';
import type { ToolActivity } from './streamReducer';
import type { ChatSession, KnowledgeMode, MessageToolInvocation } from '@/api';

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

describe('groupCitationsByDocument', () => {
  it('returns an empty array for no citations', () => {
    expect(groupCitationsByDocument([])).toEqual([]);
  });

  it('collapses many passages of one document into a single group', () => {
    const cs = [
      cite({ id: 'c1', documentId: 'd1', chunkId: 'k1' }),
      cite({ id: 'c2', documentId: 'd1', chunkId: 'k2' }),
      cite({ id: 'c3', documentId: 'd1', chunkId: 'k3' }),
    ];
    const groups = groupCitationsByDocument(cs);
    expect(groups).toHaveLength(1);
    expect(groups[0]?.passages.map((p) => p.number)).toEqual([1, 2, 3]);
  });

  it('preserves first-appearance document order and keeps FLAT numbering across groups', () => {
    const cs = [
      cite({ id: 'a1', documentId: 'A', documentName: 'A.pdf' }),
      cite({ id: 'b1', documentId: 'B', documentName: 'B.pdf' }),
      cite({ id: 'a2', documentId: 'A', documentName: 'A.pdf' }),
    ];
    const groups = groupCitationsByDocument(cs);
    expect(groups.map((g) => g.documentId)).toEqual(['A', 'B']); // first-appearance order
    // Group A keeps flat numbers 1 and 3 (not renumbered 1,2); B keeps 2.
    expect(groups[0]?.passages.map((p) => p.number)).toEqual([1, 3]);
    expect(groups[1]?.passages.map((p) => p.number)).toEqual([2]);
  });
});

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

  it('never says "0 sources · N passages" when passages arrive before citations (#248)', () => {
    // Mid-stream: the search tool finished with 10 hits, but citation events have
    // not populated yet (or the answer cited nothing). The trace must NOT read the
    // contradictory "Looked at 0 sources · 10 passages".
    const result = buildRetrievalSummary([], [doneTool({ hitCount: 10 })]);
    expect(result.summary).toBe('Looked at 10 passages');
    expect(result.summary).not.toMatch(/0 sources/);
    expect(result.hasContent).toBe(true);
  });

  it('drops the leading "0 sources" but keeps passages + excluded when uncited', () => {
    const result = buildRetrievalSummary([], [doneTool({ hitCount: 10 })], 38);
    expect(result.summary).toBe('Looked at 10 passages · 38 excluded');
    expect(result.summary).not.toMatch(/0 sources/);
  });
});

describe('toolActivityFromInvocations (#377)', () => {
  const inv = (over: Partial<MessageToolInvocation> = {}): MessageToolInvocation => ({
    id: 'i1',
    tool_name: 'list_documents',
    ok: true,
    duration_ms: 12,
    created_at: '2026-07-15T00:00:00Z',
    ...over,
  });

  it('maps a persisted invocation to a settled ToolActivity with a duration summary', () => {
    const [t] = toolActivityFromInvocations([inv()]);
    expect(t).toMatchObject({
      callId: 'i1',
      tool: 'list_documents',
      status: 'done',
      ok: true,
      summary: '12 ms',
    });
  });

  it('prefers the persisted handler result line over the duration (#377 "what it returned")', () => {
    const [t] = toolActivityFromInvocations([inv({ result_summary: '13 documents' })]);
    expect(t?.summary).toBe('13 documents');
  });

  it('formats second-scale durations as seconds', () => {
    expect(toolActivityFromInvocations([inv({ duration_ms: 1400 })])[0]?.summary).toBe('1.4 s');
  });

  it('marks a failure/denial with ok=false and the stable error code', () => {
    const [t] = toolActivityFromInvocations([
      inv({ ok: false, error: 'tool_denied', duration_ms: 0 }),
    ]);
    expect(t?.ok).toBe(false);
    expect(t?.summary).toBe('failed (tool_denied)');
  });

  it('reports an errorless failure honestly (no invented code)', () => {
    expect(toolActivityFromInvocations([inv({ ok: false, error: null })])[0]?.summary).toBe(
      'failed',
    );
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

describe('sourceMetadataRows', () => {
  it('always emits the owner / last-modified / last-indexed rows in order', () => {
    const rows = sourceMetadataRows({});
    expect(rows.map((r) => r.label)).toEqual(['Owner', 'Last modified', 'Last indexed']);
  });

  it('marks fields the wire does not carry as unknown (no fabricated values, GUARD #120)', () => {
    // Owner + last-modified are not on the chat/citation wire → honest "Not available".
    const rows = sourceMetadataRows({ lastIndexed: '2d ago' });
    const owner = rows.find((r) => r.label === 'Owner');
    const modified = rows.find((r) => r.label === 'Last modified');
    expect(owner).toEqual({ label: 'Owner', value: METADATA_UNKNOWN, unknown: true });
    expect(modified).toEqual({ label: 'Last modified', value: METADATA_UNKNOWN, unknown: true });
  });

  it('passes through a REAL source-indexing label as last-indexed', () => {
    const indexed = sourceMetadataRows({ lastIndexed: 'Indexed 2d ago' }).find(
      (r) => r.label === 'Last indexed',
    );
    expect(indexed).toEqual({ label: 'Last indexed', value: 'Indexed 2d ago', unknown: false });
  });

  it('marks last-indexed unknown when no source-indexing value is supplied (GUARD #120)', () => {
    // The chat/citation wire carries no source-indexing timestamp. The only time
    // a chat turn has is the ANSWER/message time — which is NOT source provenance.
    // With nothing real supplied, "Last indexed" must be "Not available", so a
    // doc indexed months ago can never render "Last indexed: Just now".
    const indexed = sourceMetadataRows({}).find((r) => r.label === 'Last indexed');
    expect(indexed).toEqual({ label: 'Last indexed', value: METADATA_UNKNOWN, unknown: true });
  });

  it('treats blank / whitespace values as unknown', () => {
    const rows = sourceMetadataRows({ owner: '   ', lastIndexed: '' });
    expect(rows.every((r) => r.unknown)).toBe(true);
  });
});

// --- history-sidebar presentation (#136) ---------------------------------

function session(over: Partial<ChatSession> = {}): ChatSession {
  return {
    id: 's1',
    title: 'Q3 pricing',
    model: 'openrouter/gpt-4o',
    owner_id: 'u1',
    message_count: 4,
    created_at: '2026-06-19T09:00:00Z',
    updated_at: '2026-06-19T09:00:00Z',
    ...over,
  };
}

describe('dayBucket', () => {
  const noon = Date.parse('2026-06-19T12:00:00Z');

  it('buckets by calendar day, not elapsed hours', () => {
    // 09:00 same calendar day → Today (even though it's not "just now").
    expect(dayBucket('2026-06-19T09:00:00Z', noon)).toBe('Today');
    // 11pm the night before reads as Yesterday, not "13h ago".
    expect(dayBucket('2026-06-18T23:00:00Z', noon)).toBe('Yesterday');
    expect(dayBucket('2026-06-15T10:00:00Z', noon)).toBe('Previous 7 days');
    expect(dayBucket('2026-05-01T10:00:00Z', noon)).toBe('Older');
  });

  it('falls back to Today for absent / unparseable / future timestamps', () => {
    expect(dayBucket(undefined, noon)).toBe('Today');
    expect(dayBucket('not-a-date', noon)).toBe('Today');
    expect(dayBucket('2026-07-01T10:00:00Z', noon)).toBe('Today');
  });
});

describe('groupSessionsByDay', () => {
  const noon = Date.parse('2026-06-19T12:00:00Z');

  it('groups newest-first, drops empty buckets, sorts within a bucket', () => {
    const groups = groupSessionsByDay(
      [
        session({ id: 'a', updated_at: '2026-06-19T08:00:00Z' }),
        session({ id: 'b', updated_at: '2026-06-19T10:00:00Z' }),
        session({ id: 'c', updated_at: '2026-06-15T10:00:00Z' }),
      ],
      noon,
    );
    expect(groups.map((g) => g.label)).toEqual(['Today', 'Previous 7 days']);
    // Within Today, the 10:00 session sorts ahead of the 08:00 one.
    expect(groups[0]?.sessions.map((s) => s.id)).toEqual(['b', 'a']);
  });
});

describe('sessionMeta', () => {
  const noon = Date.parse('2026-06-19T12:00:00Z');

  it('builds an honest meta line from wire fields only (no fabricated counts)', () => {
    const meta = sessionMeta(
      session({ message_count: 4, model: 'openrouter/gpt-4o', updated_at: '2026-06-19T10:00:00Z' }),
      noon,
    );
    expect(meta).toBe('4 messages · gpt-4o · 2h ago');
  });

  it('singularises one message', () => {
    expect(sessionMeta(session({ message_count: 1 }), noon)).toMatch(/^1 message ·/);
  });
});

/* ── web knowledge modes + disclosure (#221, epic E3-12) ─────────────────── */

function tool(over: Partial<ToolActivity> = {}): ToolActivity {
  return { callId: 't1', tool: 'search_text', status: 'done', ...over };
}

describe('initialModes', () => {
  it('seeds an assistant session with its declared modes', () => {
    const scope: KnowledgeMode[] = ['company', 'web'];
    expect(initialModes(scope)).toEqual(['company', 'web']);
    // A copy, not the same reference (so per-chat toggling doesn't mutate scope).
    expect(initialModes(scope)).not.toBe(scope);
  });

  it('falls back to the corpus default for an ad-hoc chat', () => {
    expect(initialModes(undefined)).toEqual([...DEFAULT_CHAT_MODES]);
    expect(initialModes([])).toEqual([...DEFAULT_CHAT_MODES]);
  });
});

describe('modeAvailability', () => {
  it('marks web AVAILABLE only when the scope includes it', () => {
    expect(modeAvailability(['company', 'web']).web?.available).toBe(true);
  });

  it('marks web UNAVAILABLE with a reason when the scope omits it (AC-3)', () => {
    const web = modeAvailability(['company']).web;
    expect(web?.available).toBe(false);
    expect(web?.reason).toMatch(/web search is off/i);
  });

  it('gives a reason that points at the real path, not a nonexistent setting (#378)', () => {
    const web = modeAvailability(undefined).web;
    // An ad-hoc chat has no assistant settings page — the reason must name the
    // actual route to web access (start a chat from an assistant that allows
    // it), never instruct the user to "turn on" a control that doesn't exist.
    expect(web?.reason).toMatch(/assistant that allows web/i);
    expect(web?.reason).not.toMatch(/turn it on/i);
  });

  it('fails web closed for an ad-hoc chat (no scope)', () => {
    expect(modeAvailability(undefined).web?.available).toBe(false);
  });
});

describe('usedWebSearch', () => {
  it('is true when a web citation is present', () => {
    expect(usedWebSearch([cite({ url: 'https://example.com/a' })], [])).toBe(true);
  });

  it('is true when the web_search tool ran (even with no citation)', () => {
    expect(usedWebSearch([], [tool({ tool: 'web_search' })])).toBe(true);
  });

  it('is false for a document-only answer', () => {
    expect(usedWebSearch([cite()], [tool({ tool: 'search_text' })])).toBe(false);
    expect(usedWebSearch([], [])).toBe(false);
  });
});

describe('partitionCitations', () => {
  it('splits web vs document citations, preserving order', () => {
    const doc = cite({ id: 'd1' });
    const web = cite({ id: 'w1', documentId: '', url: 'https://example.com/x' });
    const { web: webs, documents } = partitionCitations([doc, web]);
    expect(documents.map((c) => c.id)).toEqual(['d1']);
    expect(webs.map((c) => c.id)).toEqual(['w1']);
  });
});
