/**
 * Pure presentation helpers for the Sources slice (#27, #455) — status →
 * tone/label (incl. the managed `pending_auth`), glyph/name/detail per union
 * branch, relative time + sync/ACL freshness (with an injected `now` so the
 * assertions are deterministic), client URL validation, the create/connect
 * error mappings, and the frozen `connect=error` reason → human message map
 * (every closed reason code). No transport / DOM here.
 */
import { describe, it, expect } from 'vitest';
import { ApiError } from '@/api';
import type { GdriveSource, WebSource } from './types';
import {
  aclFreshness,
  connectReturnErrorMessage,
  connectSourceErrorMessage,
  createSourceErrorMessage,
  deleteSourceErrorMessage,
  freshness,
  gdriveScopeLabel,
  isGdriveSource,
  modeLabel,
  parseConnectErrorReason,
  relativeTime,
  safeHost,
  sourceDetail,
  sourceGlyph,
  sourceName,
  statusBadge,
  statusLabel,
  statusTone,
  validateUrl,
} from './presentation';

function makeSource(overrides: Partial<WebSource> = {}): WebSource {
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

function makeGdrive(overrides: Partial<GdriveSource> = {}): GdriveSource {
  return {
    id: 'g1',
    type: 'gdrive',
    config: { mode: 'my_drive' },
    status: 'ready',
    indexed_count: 240,
    last_synced_at: '2026-06-23T11:50:00Z',
    connected_account: { email: 'ops@acme.com' },
    acl_synced_at: '2026-06-23T11:50:00Z',
    unmapped_acl_count: 0,
    reauthorize_required: false,
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
    expect(statusTone('pending_auth')).toBe('warn');
  });

  it('labels each status', () => {
    expect(statusLabel('ready')).toMatch(/synced/i);
    expect(statusLabel('syncing')).toMatch(/syncing/i);
    expect(statusLabel('error')).toMatch(/failed/i);
    expect(statusLabel('pending')).toMatch(/queued/i);
    expect(statusLabel('pending_auth')).toMatch(/awaiting consent/i);
  });

  it('picks a badge variant per status', () => {
    expect(statusBadge('ready')).toEqual({ modifier: 'lc-badge--ok', label: 'Connected' });
    expect(statusBadge('syncing').modifier).toBe('lc-badge--info');
    expect(statusBadge('error').modifier).toBe('lc-badge--danger');
    expect(statusBadge('pending').modifier).toBe('');
    expect(statusBadge('pending_auth')).toEqual({
      modifier: 'lc-badge--warn',
      label: 'Awaiting consent',
    });
  });
});

describe('glyph / name / detail / mode / host', () => {
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

  it('gives a gdrive source a fixed glyph + connector name', () => {
    expect(sourceGlyph(makeGdrive())).toBe('GD');
    expect(sourceName(makeGdrive())).toBe('Google Drive');
    expect(isGdriveSource(makeGdrive())).toBe(true);
    expect(isGdriveSource(makeSource())).toBe(false);
  });

  it('details a web source by URL and a gdrive source by its synced scope', () => {
    expect(sourceDetail(makeSource())).toBe('https://handbook.acme.com/policy');
    expect(sourceDetail(makeGdrive())).toBe('My Drive');
    expect(gdriveScopeLabel({ mode: 'folder', folder_id: 'f-1' })).toBe('Folder f-1');
    expect(gdriveScopeLabel({ mode: 'folder', folder_id: 'f-1', drive_id: 'd-9' })).toBe(
      'Folder f-1 in Shared Drive d-9',
    );
    expect(gdriveScopeLabel({ mode: 'shared_drive', drive_id: 'd-9' })).toBe('Shared Drive d-9');
  });

  it('labels the web mode', () => {
    expect(modeLabel(makeSource({ config: { url: 'x', mode: 'feed' } }))).toMatch(/feed/i);
    expect(modeLabel(makeSource({ config: { url: 'x', mode: 'sitemap' } }))).toMatch(/sitemap/i);
    expect(modeLabel(makeSource({ config: { url: 'x', mode: 'page' } }))).toMatch(/web page/i);
  });

  it('labels the gdrive modes as read-only Drive scopes', () => {
    expect(modeLabel(makeGdrive())).toMatch(/my drive.*read-only/i);
    expect(modeLabel(makeGdrive({ config: { mode: 'folder', folder_id: 'f' } }))).toMatch(
      /folder.*read-only/i,
    );
    expect(modeLabel(makeGdrive({ config: { mode: 'shared_drive', drive_id: 'd' } }))).toMatch(
      /shared drive.*read-only/i,
    );
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

  it('marks a pending_auth source as awaiting consent', () => {
    expect(
      freshness(makeGdrive({ status: 'pending_auth', last_synced_at: null }), now),
    ).toEqual({ label: 'Awaiting consent', stale: false });
  });

  it('flags a ready source synced over a day ago as stale', () => {
    const fresh = freshness(makeSource({ last_synced_at: '2026-06-23T11:50:00Z' }), now);
    expect(fresh).toEqual({ label: 'Synced 10m ago', stale: false });
    const old = freshness(makeSource({ last_synced_at: '2026-06-20T12:00:00Z' }), now);
    expect(old?.stale).toBe(true);
  });
});

describe('aclFreshness (managed sources, ADR-0019 §2)', () => {
  const now = Date.parse('2026-06-23T12:00:00Z');

  it('labels a fresh mirror and flags one older than the window as stale', () => {
    expect(aclFreshness(makeGdrive({ acl_synced_at: '2026-06-23T11:50:00Z' }), now)).toEqual({
      label: 'Permissions mirrored 10m ago',
      stale: false,
    });
    expect(aclFreshness(makeGdrive({ acl_synced_at: '2026-06-20T12:00:00Z' }), now).stale).toBe(
      true,
    );
  });

  it('labels a never-mirrored source honestly (null before the first sync)', () => {
    expect(aclFreshness(makeGdrive({ acl_synced_at: null }), now)).toEqual({
      label: 'Permissions not yet mirrored',
      stale: false,
    });
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

  it('explains an invalid gdrive config (422 code=invalid_config)', () => {
    const err = new ApiError('x', 422, {
      type: 'about:blank',
      title: 'Unprocessable',
      status: 422,
      code: 'invalid_config',
    });
    expect(createSourceErrorMessage(err)).toMatch(/drive configuration/i);
  });

  it('messages the managed-source admin gate on a 403 (INV-5)', () => {
    expect(createSourceErrorMessage(new ApiError('x', 403))).toMatch(/tenant admin/i);
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

describe('deleteSourceErrorMessage', () => {
  it('maps 403 to the action-time admin/role gate (INV-5), 404 to already-gone, 401 to session expiry', () => {
    expect(deleteSourceErrorMessage(new ApiError('x', 403))).toMatch(/tenant admin.*role may have changed/i);
    expect(deleteSourceErrorMessage(new ApiError('x', 404))).toMatch(/no longer exists/i);
    expect(deleteSourceErrorMessage(new ApiError('x', 401))).toMatch(/session expired/i);
  });
});

describe('connectSourceErrorMessage', () => {
  it('maps 403 to the admin gate (INV-5), 409 to not-connectable, 404 to gone', () => {
    expect(connectSourceErrorMessage(new ApiError('x', 403))).toMatch(/tenant admin/i);
    expect(connectSourceErrorMessage(new ApiError('x', 409))).toMatch(/can't start a consent/i);
    expect(connectSourceErrorMessage(new ApiError('x', 404))).toMatch(/no longer exists/i);
  });
});

describe('connect return reason mapping (the frozen callback contract)', () => {
  it('narrows only the closed reason set', () => {
    expect(parseConnectErrorReason('expired')).toBe('expired');
    expect(parseConnectErrorReason('denied')).toBe('denied');
    expect(parseConnectErrorReason('provider_error')).toBe('provider_error');
    expect(parseConnectErrorReason('failed')).toBe('failed');
    expect(parseConnectErrorReason('made_up')).toBeNull();
    expect(parseConnectErrorReason(null)).toBeNull();
  });

  it('maps EVERY closed reason code to a distinct human message', () => {
    expect(connectReturnErrorMessage('expired')).toMatch(/expired or was already used/i);
    expect(connectReturnErrorMessage('denied')).toMatch(/not authorized/i);
    expect(connectReturnErrorMessage('provider_error')).toMatch(/google reported a problem/i);
    expect(connectReturnErrorMessage('failed')).toMatch(/something went wrong/i);
    // Unknown/missing reason falls back to the generic failure copy — never blank.
    expect(connectReturnErrorMessage(null)).toMatch(/something went wrong/i);
  });
});
