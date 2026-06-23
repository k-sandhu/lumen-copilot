/**
 * Pure presentation helpers for the Sources slice (#27) — status → tone/label,
 * glyph/name from URL, relative time + freshness (with an injected `now` so the
 * assertions are deterministic), client URL validation, and the create-error
 * mapping (422 url_blocked vs. generic). No transport / DOM here.
 */
import { describe, it, expect } from 'vitest';
import { ApiError } from '@/api';
import type { Source } from './types';
import {
  createSourceErrorMessage,
  freshness,
  modeLabel,
  relativeTime,
  safeHost,
  sourceGlyph,
  sourceName,
  statusBadge,
  statusLabel,
  statusTone,
  validateUrl,
} from './presentation';

function makeSource(overrides: Partial<Source> = {}): Source {
  return {
    id: 's1',
    type: 'web',
    config: { url: 'https://handbook.acme.com/policy', mode: 'page' },
    status: 'ready',
    indexed_count: 12,
    last_synced_at: '2026-06-23T11:50:00Z',
    owner_id: 'u1',
    created_at: '2026-06-23T10:00:00Z',
    updated_at: '2026-06-23T11:50:00Z',
    ...overrides,
  };
}

describe('statusTone / statusLabel / statusBadge', () => {
  it('maps each lifecycle status to its tone', () => {
    expect(statusTone('ready')).toBe('ok');
    expect(statusTone('syncing')).toBe('sync');
    expect(statusTone('error')).toBe('danger');
    expect(statusTone('pending')).toBe('muted');
  });

  it('labels each status', () => {
    expect(statusLabel('ready')).toMatch(/synced/i);
    expect(statusLabel('syncing')).toMatch(/syncing/i);
    expect(statusLabel('error')).toMatch(/failed/i);
    expect(statusLabel('pending')).toMatch(/queued/i);
  });

  it('picks a badge variant per status', () => {
    expect(statusBadge('ready')).toEqual({ modifier: 'lc-badge--ok', label: 'Connected' });
    expect(statusBadge('syncing').modifier).toBe('lc-badge--info');
    expect(statusBadge('error').modifier).toBe('lc-badge--danger');
    expect(statusBadge('pending').modifier).toBe('');
  });
});

describe('glyph / name / mode / host', () => {
  it('derives a 2-letter glyph from the URL host (www-stripped, uppercased)', () => {
    expect(sourceGlyph(makeSource({ config: { url: 'https://www.notion.so/x', mode: 'page' } }))).toBe(
      'NO',
    );
  });

  it('falls back to WB when the URL has no parseable host', () => {
    expect(sourceGlyph(makeSource({ config: { url: 'not a url', mode: 'page' } }))).toBe('WB');
  });

  it('names the source by host, falling back to the raw URL', () => {
    expect(sourceName(makeSource())).toBe('handbook.acme.com');
    expect(sourceName(makeSource({ config: { url: 'garbage', mode: 'page' } }))).toBe('garbage');
  });

  it('labels the web mode', () => {
    expect(modeLabel(makeSource({ config: { url: 'x', mode: 'feed' } }))).toMatch(/feed/i);
    expect(modeLabel(makeSource({ config: { url: 'x', mode: 'sitemap' } }))).toMatch(/sitemap/i);
    expect(modeLabel(makeSource({ config: { url: 'x', mode: 'page' } }))).toMatch(/web page/i);
  });

  it('safeHost returns null for non-URLs', () => {
    expect(safeHost('https://a.com/b')).toBe('a.com');
    expect(safeHost('::::')).toBeNull();
  });
});

describe('relativeTime / freshness', () => {
  const now = Date.parse('2026-06-23T12:00:00Z');

  it('formats minutes / hours / days ago', () => {
    expect(relativeTime('2026-06-23T11:52:00Z', now)).toBe('8m ago');
    expect(relativeTime('2026-06-23T09:00:00Z', now)).toBe('3h ago');
    expect(relativeTime('2026-06-21T12:00:00Z', now)).toBe('2d ago');
  });

  it('returns "just now" for very recent / future stamps and null for junk', () => {
    expect(relativeTime('2026-06-23T11:59:50Z', now)).toBe('just now');
    expect(relativeTime('2026-06-23T12:05:00Z', now)).toBe('just now');
    expect(relativeTime(null, now)).toBeNull();
    expect(relativeTime('nonsense', now)).toBeNull();
  });

  it('marks a pending source as not-yet-synced', () => {
    expect(freshness(makeSource({ status: 'pending', last_synced_at: null }), now)).toEqual({
      label: 'Not yet synced',
      stale: false,
    });
  });

  it('flags a ready source synced over a day ago as stale', () => {
    const fresh = freshness(makeSource({ last_synced_at: '2026-06-23T11:50:00Z' }), now);
    expect(fresh).toEqual({ label: 'Synced 10m ago', stale: false });
    const old = freshness(makeSource({ last_synced_at: '2026-06-20T12:00:00Z' }), now);
    expect(old?.stale).toBe(true);
  });
});

describe('validateUrl (client-side UX guard)', () => {
  it('accepts an http(s) absolute URL and returns the trimmed value', () => {
    expect(validateUrl('  https://example.com/x  ')).toEqual({
      ok: true,
      url: 'https://example.com/x',
    });
    expect(validateUrl('http://example.com').ok).toBe(true);
  });

  it('rejects empty, non-URL, and non-http schemes', () => {
    expect(validateUrl('   ').ok).toBe(false);
    expect(validateUrl('not a url').ok).toBe(false);
    expect(validateUrl('ftp://example.com').ok).toBe(false);
    expect(validateUrl('file:///etc/passwd').ok).toBe(false);
  });

  it('explains the non-http rejection', () => {
    expect(validateUrl('ftp://example.com').error).toMatch(/http:\/\/ and https:\/\//i);
  });
});

describe('createSourceErrorMessage', () => {
  it('explains an SSRF block (422 code=url_blocked) plainly', () => {
    const err = new ApiError('x', 422, {
      type: 'about:blank',
      title: 'Unprocessable',
      status: 422,
      code: 'url_blocked',
    });
    expect(createSourceErrorMessage(err)).toMatch(/blocked or private address/i);
  });

  it('surfaces a field error / detail on a generic 422', () => {
    const withField = new ApiError('x', 422, {
      type: 'about:blank',
      title: 'Unprocessable',
      status: 422,
      errors: [{ field: 'url', message: 'URL is not reachable.' }],
    });
    expect(createSourceErrorMessage(withField)).toBe('URL is not reachable.');
  });

  it('messages a 401 as a session-expiry', () => {
    expect(createSourceErrorMessage(new ApiError('x', 401))).toMatch(/session expired/i);
  });
});
