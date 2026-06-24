import { describe, it, expect } from 'vitest';
import {
  statusTone,
  statusLabel,
  isIngesting,
  formatBytes,
  statusDotTone,
  fileKind,
  fileKindTone,
  ingestSteps,
  relativeTime,
  documentFreshness,
  ownerLabel,
  visibility,
  DOC_STALE_AFTER_MS,
} from './presentation';

describe('document presentation helpers', () => {
  it('maps status → tone', () => {
    expect(statusTone('ready')).toBe('ok');
    expect(statusTone('failed')).toBe('danger');
    expect(statusTone('pending')).toBe('pending');
    expect(statusTone('processing')).toBe('pending');
  });

  it('labels each status', () => {
    expect(statusLabel('pending')).toBe('Queued');
    expect(statusLabel('processing')).toBe('Processing');
    expect(statusLabel('ready')).toBe('Ready');
    expect(statusLabel('failed')).toBe('Failed');
  });

  it('detects in-progress ingestion', () => {
    expect(isIngesting('pending')).toBe(true);
    expect(isIngesting('processing')).toBe(true);
    expect(isIngesting('ready')).toBe(false);
    expect(isIngesting('failed')).toBe(false);
  });

  it('formats bytes compactly', () => {
    expect(formatBytes(0)).toBe('0 B');
    expect(formatBytes(512)).toBe('512 B');
    expect(formatBytes(1024)).toBe('1 KB');
    expect(formatBytes(1536)).toBe('1.5 KB');
    expect(formatBytes(1024 * 1024)).toBe('1 MB');
    expect(formatBytes(25 * 1024 * 1024)).toBe('25 MB');
    expect(formatBytes(-1)).toBe('—');
  });
});

describe('#89 trust-signal helpers', () => {
  it('maps status → kit StatusDot tone (sync pulses while processing)', () => {
    expect(statusDotTone('ready')).toBe('ok');
    expect(statusDotTone('failed')).toBe('danger');
    expect(statusDotTone('processing')).toBe('sync');
    expect(statusDotTone('pending')).toBe('muted');
  });

  it('derives a file-kind tag from the filename, then the mime', () => {
    expect(fileKind({ filename: 'Vendor-MSA.pdf', mime_type: 'application/pdf' })).toBe('PDF');
    expect(fileKind({ filename: 'plan.xlsx', mime_type: 'application/octet-stream' })).toBe('XLSX');
    expect(fileKind({ filename: 'noext', mime_type: 'text/markdown' })).toBe('MARKDOWN');
  });

  describe('ingestSteps (parse → chunk → embed → ready)', () => {
    it('marks all stages done once ready, with the chunk count', () => {
      const steps = ingestSteps({ status: 'ready', chunk_count: 142 });
      expect(steps.map((s) => s.state)).toEqual(['done', 'done', 'done', 'done']);
      expect(steps[1]?.label).toBe('Chunked into 142 passages');
    });

    it('shows embed as the active stage while processing (chunks counted)', () => {
      const steps = ingestSteps({ status: 'processing', chunk_count: 88 });
      // parse + chunk done, embed in flight, ready pending.
      expect(steps.map((s) => s.state)).toEqual(['done', 'done', 'active', 'pending']);
    });

    it('treats pending as nothing-started (parse pending, none active)', () => {
      const steps = ingestSteps({ status: 'pending', chunk_count: 0 });
      expect(steps.map((s) => s.state)).toEqual(['pending', 'pending', 'pending', 'pending']);
    });

    it('marks the first incomplete stage as failed', () => {
      // failed before chunks counted → parse stage failed.
      const early = ingestSteps({ status: 'failed', chunk_count: 0 });
      expect(early[0]?.state).toBe('failed');
      // failed after chunks → embed stage failed.
      const late = ingestSteps({ status: 'failed', chunk_count: 12 });
      expect(late.map((s) => s.state)).toEqual(['done', 'done', 'failed', 'pending']);
    });
  });
});

describe('#119 documents-table helpers', () => {
  describe('fileKindTone', () => {
    it('groups common types into visual families', () => {
      expect(fileKindTone({ filename: 'a.pdf', mime_type: 'application/pdf' })).toBe('pdf');
      expect(
        fileKindTone({
          filename: 'a.docx',
          mime_type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        }),
      ).toBe('doc');
      expect(fileKindTone({ filename: 'a.xlsx', mime_type: 'application/octet-stream' })).toBe(
        'sheet',
      );
      expect(fileKindTone({ filename: 'a.csv', mime_type: 'text/csv' })).toBe('sheet');
      expect(fileKindTone({ filename: 'a.pptx', mime_type: 'application/octet-stream' })).toBe(
        'slide',
      );
      expect(fileKindTone({ filename: 'a.png', mime_type: 'image/png' })).toBe('image');
      expect(fileKindTone({ filename: 'a.md', mime_type: 'text/markdown' })).toBe('text');
    });

    it('falls back to a neutral default for unknown types', () => {
      expect(fileKindTone({ filename: 'a.bin', mime_type: 'application/octet-stream' })).toBe(
        'default',
      );
    });
  });

  describe('relativeTime', () => {
    const now = Date.parse('2026-06-20T12:00:00Z');
    it('renders compact relative labels', () => {
      expect(relativeTime('2026-06-20T11:59:50Z', now)).toBe('just now');
      expect(relativeTime('2026-06-20T11:30:00Z', now)).toBe('30m ago');
      expect(relativeTime('2026-06-20T10:00:00Z', now)).toBe('2h ago');
      expect(relativeTime('2026-06-17T12:00:00Z', now)).toBe('3d ago');
    });
    it('returns null for a missing/unparseable stamp', () => {
      expect(relativeTime(undefined, now)).toBeNull();
      expect(relativeTime('not-a-date', now)).toBeNull();
    });
  });

  describe('documentFreshness', () => {
    const now = Date.parse('2026-06-20T12:00:00Z');
    it('derives a label + stale flag from updated_at', () => {
      const fresh = documentFreshness({ updated_at: '2026-06-20T10:00:00Z' }, now);
      expect(fresh).toEqual({ label: '2h ago', stale: false, title: '2026-06-20T10:00:00Z' });
    });
    it('marks updates older than the freshness window as stale', () => {
      const old = new Date(now - DOC_STALE_AFTER_MS - 1000).toISOString();
      expect(documentFreshness({ updated_at: old }, now)?.stale).toBe(true);
    });
    it('returns null for a missing stamp', () => {
      expect(documentFreshness({ updated_at: '' }, now)).toBeNull();
    });
  });

  describe('ownerLabel', () => {
    it('reads "You" for the current user’s own document', () => {
      expect(ownerLabel({ owner_id: 'u-1' }, 'u-1')).toBe('You');
    });
    it('shows an honest short id for another owner (never a fabricated name)', () => {
      expect(ownerLabel({ owner_id: 'abc12345-def6-7890' }, 'u-1')).toBe('User abc12345');
      expect(ownerLabel({ owner_id: 'plainid000000' }, 'u-1')).toBe('User plainid0');
    });
  });

  describe('visibility', () => {
    it('renders the owner-only invariant (no invented visibility levels)', () => {
      const mine = visibility({ owner_id: 'u-1' }, 'u-1');
      expect(mine.level).toBe('granted');
      expect(mine.label).toBe('Private to you');

      const theirs = visibility({ owner_id: 'other' }, 'u-1');
      expect(theirs.level).toBe('restricted');
      expect(theirs.label).toBe('Owner only');
    });
  });
});
