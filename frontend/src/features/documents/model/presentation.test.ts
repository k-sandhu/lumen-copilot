import { describe, it, expect } from 'vitest';
import {
  statusTone,
  statusLabel,
  isIngesting,
  formatBytes,
  statusDotTone,
  fileKind,
  ingestSteps,
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
