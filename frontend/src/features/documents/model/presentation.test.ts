import { describe, it, expect } from 'vitest';
import { statusTone, statusLabel, isIngesting, formatBytes } from './presentation';

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
