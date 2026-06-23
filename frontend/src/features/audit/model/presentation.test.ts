/**
 * Unit tests for the audit presentation mapping (#86). Pure functions — no React,
 * no I/O — verifying the wire→kit translation, including the INVARIANT that
 * excluded candidates survive into the drawer (a permission trim must be provable
 * after the fact, mission filter #4 / spec 0004 §2.4).
 */
import { describe, it, expect } from 'vitest';
import type { AuditEvent } from '@/api';
import {
  kindForEventType,
  eventTypeLabel,
  shortId,
  toKitRow,
  toProvenanceDetail,
} from './presentation';

const baseEvent: AuditEvent = {
  id: 'evt_0123456789abcdef',
  ts: '2026-06-19T10:00:00Z',
  actor: 'user-1',
  tenant_id: 't1',
  event_type: 'retrieval.query',
  resource_id: null,
  decision: 'allowed',
  provenance: {
    candidates: [
      { resource_id: 'p1', disposition: 'allow', reason: 'in allow-set', score: 0.91 },
      { resource_id: 'p2', disposition: 'exclude', reason: 'owner mismatch' },
    ],
    raw: { query_hash: 'abc123' },
  },
};

describe('kindForEventType', () => {
  it('folds the wire taxonomy into the four kit kinds', () => {
    expect(kindForEventType('retrieval.query')).toBe('retrieval');
    expect(kindForEventType('answer.generated')).toBe('answer');
    expect(kindForEventType('permission.denied')).toBe('access');
    expect(kindForEventType('auth.login')).toBe('access');
    expect(kindForEventType('action.executed')).toBe('action');
    expect(kindForEventType('document.uploaded')).toBe('action');
  });
});

describe('eventTypeLabel', () => {
  it('gives a human label for each event type', () => {
    expect(eventTypeLabel('permission.denied')).toBe('Permission denied');
    expect(eventTypeLabel('retrieval.query')).toBe('Retrieval');
  });
});

describe('shortId', () => {
  it('truncates long ids and leaves short ids alone', () => {
    expect(shortId('evt_0123456789abcdef')).toBe('evt_01234567…');
    expect(shortId('evt_9f3a')).toBe('evt_9f3a');
  });
});

describe('toKitRow', () => {
  it('maps actor + decision (+ resource) into the secondary line', () => {
    const row = toKitRow(baseEvent);
    expect(row.kind).toBe('retrieval');
    expect(row.action).toBe('Retrieval');
    expect(row.detail).toContain('user-1');
    expect(row.detail).toContain('allowed');
  });

  it('includes the resource id when present', () => {
    const row = toKitRow({ ...baseEvent, resource_id: 'doc-9', decision: 'denied' });
    expect(row.detail).toContain('doc-9');
    expect(row.detail).toContain('denied');
  });
});

describe('toProvenanceDetail', () => {
  it('carries the metadata, candidate ledger, and raw payload', () => {
    const detail = toProvenanceDetail(baseEvent);
    expect(detail.id).toBe('evt_0123456789abcdef');
    expect(detail.meta?.some((m) => m.key === 'Actor' && m.value === 'user-1')).toBe(true);
    expect(detail.meta?.some((m) => m.key === 'Tenant' && m.value === 't1')).toBe(true);
    expect(detail.candidates).toHaveLength(2);
    expect(detail.raw).toEqual({ query_hash: 'abc123' });
  });

  it('preserves EXCLUDED candidates so a permission trim is provable (filter #4)', () => {
    const detail = toProvenanceDetail(baseEvent);
    const excluded = detail.candidates?.find((c) => c.decision === 'excluded');
    expect(excluded).toBeDefined();
    expect(excluded?.title).toBe('p2');
    expect(excluded?.reason).toContain('owner mismatch');
  });

  it('appends the score to the candidate reason when present', () => {
    const detail = toProvenanceDetail(baseEvent);
    const allowed = detail.candidates?.find((c) => c.decision === 'allowed');
    expect(allowed?.reason).toContain('score');
  });

  it('defaults raw to an empty object when the wire omits it', () => {
    const detail = toProvenanceDetail({
      ...baseEvent,
      provenance: { candidates: [] },
    });
    expect(detail.raw).toEqual({});
  });
});
