/**
 * Unit tests for the audit client-side analytics (#121). Pure functions — no
 * React, no I/O — covering the honesty guarantees: KPIs are derived only from
 * the page of events, an empty page yields no fabricated grounding rate, the
 * segmented fold matches the wireframe chips, and the CSV is RFC-4180 safe.
 */
import { describe, it, expect } from 'vitest';
import type { AuditEvent } from '@/api';
import {
  summarizeEvents,
  formatRate,
  filterBySegment,
  segmentForEvent,
  eventsToCsv,
} from './metrics';

function ev(partial: Partial<AuditEvent> & { id: string }): AuditEvent {
  return {
    ts: '2026-06-19T10:00:00Z',
    actor: 'user-1',
    tenant_id: 't1',
    event_type: 'retrieval.query',
    resource_id: null,
    decision: 'allowed',
    provenance: { candidates: [] },
    ...partial,
  };
}

const cited = (id: string): AuditEvent =>
  ev({
    id,
    event_type: 'answer.generated',
    provenance: { candidates: [{ resource_id: 'p1', disposition: 'allow', reason: 'rank 1' }] },
  });

const uncitedAnswer = (id: string): AuditEvent =>
  ev({ id, event_type: 'answer.generated', provenance: { candidates: [] } });

const denied = (id: string): AuditEvent =>
  ev({ id, event_type: 'permission.denied', decision: 'denied' });

describe('summarizeEvents', () => {
  it('counts total, denied, and the cited share of answers', () => {
    const m = summarizeEvents([cited('a'), uncitedAnswer('b'), denied('c'), ev({ id: 'd' })]);
    expect(m.total).toBe(4);
    expect(m.denied).toBe(1);
    expect(m.answers).toBe(2);
    expect(m.answersCited).toBe(1);
    expect(m.citedRate).toBeCloseTo(0.5);
  });

  it('reports a null cited rate when there are no answers (never a fake 0%)', () => {
    const m = summarizeEvents([denied('a'), ev({ id: 'b' })]);
    expect(m.answers).toBe(0);
    expect(m.citedRate).toBeNull();
  });

  it('is empty for an empty page', () => {
    const m = summarizeEvents([]);
    expect(m).toMatchObject({ total: 0, denied: 0, answers: 0, answersCited: 0, citedRate: null });
  });
});

describe('formatRate', () => {
  it('renders an em-dash for no data and a percent otherwise', () => {
    expect(formatRate(null)).toBe('—');
    expect(formatRate(1)).toBe('100%');
    expect(formatRate(0.5)).toBe('50.0%');
  });
});

describe('segmentForEvent / filterBySegment', () => {
  it('folds wire types into segments', () => {
    expect(segmentForEvent(ev({ id: '1', event_type: 'retrieval.query' }))).toBe('retrieval');
    expect(segmentForEvent(ev({ id: '2', event_type: 'answer.generated' }))).toBe('answer');
    expect(segmentForEvent(ev({ id: '3', event_type: 'action.executed' }))).toBe('action');
    expect(segmentForEvent(ev({ id: '4', event_type: 'permission.denied' }))).toBe('access');
  });

  it('passes everything through for the "all" segment', () => {
    const page = [cited('a'), denied('b')];
    expect(filterBySegment(page, 'all')).toHaveLength(2);
  });

  it('filters to a single segment client-side', () => {
    const page = [cited('a'), ev({ id: 'b', event_type: 'retrieval.query' }), denied('c')];
    expect(filterBySegment(page, 'answer').map((e) => e.id)).toEqual(['a']);
    expect(filterBySegment(page, 'retrieval').map((e) => e.id)).toEqual(['b']);
  });

  it('"access" matches denied decisions, not every login that folds to access', () => {
    const page = [
      ev({ id: 'login', event_type: 'auth.login', decision: 'allowed' }),
      denied('deny'),
    ];
    expect(filterBySegment(page, 'access').map((e) => e.id)).toEqual(['deny']);
  });
});

describe('eventsToCsv', () => {
  it('emits a header plus one row per event with candidate counts', () => {
    const csv = eventsToCsv([
      ev({
        id: 'evt_1',
        actor: 'dana@acme',
        resource_id: 'doc-9',
        event_type: 'answer.generated',
        provenance: {
          candidates: [
            { resource_id: 'p1', disposition: 'allow', reason: 'rank 1' },
            { resource_id: 'p2', disposition: 'exclude', reason: 'no access' },
          ],
        },
      }),
    ]);
    const [header, row, ...rest] = csv.split('\r\n');
    expect(header).toContain('id,ts,actor');
    expect(rest).toHaveLength(0);
    expect(row).toContain('evt_1');
    expect(row).toContain('dana@acme');
    expect(row).toContain('doc-9');
    // candidates_allowed,candidates_excluded
    expect(row?.endsWith('1,1')).toBe(true);
  });

  it('quotes fields containing commas or quotes (RFC 4180)', () => {
    const csv = eventsToCsv([ev({ id: 'evt_2', resource_id: 'a,"b"', event_type: 'retrieval.query' })]);
    expect(csv).toContain('"a,""b"""');
  });

  it('renders just the header for an empty page', () => {
    expect(eventsToCsv([])).toBe(
      'id,ts,actor,tenant_id,event_type,resource_id,decision,candidates_allowed,candidates_excluded',
    );
  });
});
