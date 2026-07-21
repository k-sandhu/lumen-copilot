/**
 * Pure presentation helpers for the artifacts slice (#222) — formatting + the
 * preview-capability decision (AC-2). No I/O, no React.
 */
import { describe, it, expect } from 'vitest';
import {
  fileKind,
  formatBytes,
  isMarkdown,
  previewKind,
  producedByLabel,
  relativeTime,
} from './presentation';

describe('formatBytes', () => {
  it('formats byte magnitudes with a compact unit', () => {
    expect(formatBytes(0)).toBe('0 B');
    expect(formatBytes(512)).toBe('512 B');
    expect(formatBytes(2048)).toBe('2 KB');
    expect(formatBytes(1_500_000)).toBe('1.4 MB');
  });
  it('is defensive about invalid sizes', () => {
    expect(formatBytes(-1)).toBe('—');
    expect(formatBytes(Number.NaN)).toBe('—');
  });
});

describe('fileKind', () => {
  it('prefers the filename extension, else the mime subtype', () => {
    expect(fileKind({ filename: 'out.csv', mime_type: 'text/csv' })).toBe('CSV');
    expect(fileKind({ filename: 'noext', mime_type: 'image/png' })).toBe('PNG');
  });
});

describe('producedByLabel', () => {
  it('maps each write origin to a human label', () => {
    expect(producedByLabel('chat_session')).toBe('Chat');
    expect(producedByLabel('run')).toBe('Run');
    expect(producedByLabel('tool')).toBe('Tool');
  });
});

describe('previewKind (AC-2)', () => {
  it('previews raster images inline', () => {
    expect(previewKind('image/png')).toBe('image');
    expect(previewKind('image/jpeg')).toBe('image');
  });
  it('previews text/markdown/csv/json as sanitized text', () => {
    expect(previewKind('text/plain')).toBe('text');
    expect(previewKind('text/markdown')).toBe('text');
    expect(previewKind('text/csv')).toBe('text');
    expect(previewKind('application/json')).toBe('text');
  });
  it('routes svg + html + office + unknown to a download card (never inline active content)', () => {
    expect(previewKind('image/svg+xml')).toBe('download');
    expect(previewKind('text/html')).toBe('download');
    expect(previewKind('application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')).toBe(
      'download',
    );
    expect(previewKind('application/octet-stream')).toBe('download');
  });
  it('ignores a charset parameter on the mime type', () => {
    expect(previewKind('text/csv; charset=utf-8')).toBe('text');
  });
});

describe('isMarkdown', () => {
  it('is true only for markdown', () => {
    expect(isMarkdown('text/markdown')).toBe(true);
    expect(isMarkdown('text/plain')).toBe(false);
    expect(isMarkdown('text/csv')).toBe(false);
  });
});

describe('relativeTime', () => {
  const now = Date.parse('2026-07-03T00:00:00Z');
  it('formats recent stamps relative to now', () => {
    expect(relativeTime('2026-07-03T00:00:00Z', now)).toBe('just now');
    expect(relativeTime('2026-07-02T22:00:00Z', now)).toBe('2h ago');
    expect(relativeTime('2026-06-30T00:00:00Z', now)).toBe('3d ago');
  });
  it('returns null for a missing/unparseable stamp', () => {
    expect(relativeTime(undefined, now)).toBeNull();
    expect(relativeTime('not-a-date', now)).toBeNull();
  });
});
