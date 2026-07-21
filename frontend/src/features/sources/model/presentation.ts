/**
 * Pure presentation helpers for the Sources slice (#27, ADR-0009; #455,
 * ADR-0019). Wire `Source` shapes → the trust-signal vocabulary the screen
 * renders (StatusDot tone + label, freshness, the connector glyph, a human
 * URL/config line), the client-side URL validation that gates the Add-source
 * form, and the OAuth-return reason → human message mapping (the frozen
 * `connect=error&reason=…` closed set).
 *
 * Kept side-effect-free and framework-free so they unit-test in isolation; the
 * api/ boundary is never touched here (no transport).
 */
import type { ApiError } from '@/api';
import type {
  ConnectErrorReason,
  GdriveSource,
  GdriveSourceConfig,
  Source,
  SourceStatus,
  StatusTone,
} from './types';

/** Narrow a `Source` to its managed `gdrive` branch (the discriminant is `type`). */
export function isGdriveSource(source: Source): source is GdriveSource {
  return source.type === 'gdrive';
}

/** Map a sync status to a StatusDot tone (ADR-0009 §4 + ADR-0019 §1 lifecycle). */
export function statusTone(status: SourceStatus): StatusTone {
  switch (status) {
    case 'ready':
      return 'ok';
    case 'syncing':
      return 'sync';
    case 'error':
      return 'danger';
    case 'pending_auth':
      return 'warn';
    case 'pending':
    default:
      return 'muted';
  }
}

/** A short status verb for the dot label + the card badge. */
export function statusLabel(status: SourceStatus): string {
  switch (status) {
    case 'ready':
      return 'Synced';
    case 'syncing':
      return 'Syncing…';
    case 'error':
      return 'Sync failed';
    case 'pending_auth':
      return 'Awaiting consent';
    case 'pending':
    default:
      return 'Queued';
  }
}

/** Badge variant (lc-badge--*) mirroring the wireframe's connected/ syncing/ failed chips. */
export function statusBadge(status: SourceStatus): { modifier: string; label: string } {
  switch (status) {
    case 'ready':
      return { modifier: 'lc-badge--ok', label: 'Connected' };
    case 'syncing':
      return { modifier: 'lc-badge--info', label: 'Syncing' };
    case 'error':
      return { modifier: 'lc-badge--danger', label: 'Error' };
    case 'pending_auth':
      return { modifier: 'lc-badge--warn', label: 'Awaiting consent' };
    case 'pending':
    default:
      return { modifier: '', label: 'Pending' };
  }
}

/**
 * A 2-letter glyph for the connector card (the wireframe's `.src` tile). For a
 * `web` source we key off the URL host's first letters so cards stay visually
 * distinct without per-vendor branding; the managed `gdrive` connector gets a
 * fixed glyph.
 */
export function sourceGlyph(source: Source): string {
  if (isGdriveSource(source)) return 'GD';
  const host = safeHost(source.config.url);
  if (!host) return 'WB';
  const bare = host.replace(/^www\./, '');
  const letters = bare.replace(/[^a-z0-9]/gi, '');
  return (letters.slice(0, 2) || 'WB').toUpperCase();
}

/**
 * The display name for a source card — the URL host for `web` (falling back to
 * the raw URL), the connector name for `gdrive`.
 */
export function sourceName(source: Source): string {
  if (isGdriveSource(source)) return 'Google Drive';
  return safeHost(source.config.url) ?? source.config.url;
}

/**
 * The secondary line under the card title: the URL for `web`, the synced scope
 * for `gdrive` (My Drive / a folder / a Shared Drive — the closed
 * mode-discriminated config, ADR-0019 §5).
 */
export function sourceDetail(source: Source): string {
  if (!isGdriveSource(source)) return source.config.url;
  return gdriveScopeLabel(source.config);
}

/** Human label for a gdrive config's synced scope. */
export function gdriveScopeLabel(config: GdriveSourceConfig): string {
  switch (config.mode) {
    case 'my_drive':
      return 'My Drive';
    case 'folder':
      return config.drive_id
        ? `Folder ${config.folder_id} in Shared Drive ${config.drive_id}`
        : `Folder ${config.folder_id}`;
    case 'shared_drive':
      return `Shared Drive ${config.drive_id}`;
  }
}

/** Human label for the connector mode line. */
export function modeLabel(source: Source): string {
  if (isGdriveSource(source)) {
    switch (source.config.mode) {
      case 'my_drive':
        return 'Google Drive · My Drive · Read-only';
      case 'folder':
        return 'Google Drive · Folder · Read-only';
      case 'shared_drive':
        return 'Google Drive · Shared Drive · Read-only';
    }
  }
  switch (source.config.mode) {
    case 'feed':
      return 'Feed · Read-only';
    case 'sitemap':
      return 'Sitemap · Read-only';
    case 'page':
    default:
      return 'Web page · Read-only';
  }
}

/** Parse a host out of a URL without throwing; `null` if it isn't a URL. */
export function safeHost(url: string): string | null {
  try {
    return new URL(url).host;
  } catch {
    return null;
  }
}

/**
 * Relative-time label, e.g. "8m ago" / "3h ago" / "just now". `now` is injectable
 * so the unit tests are deterministic. Returns `null` for a missing/future stamp.
 */
export function relativeTime(
  iso: string | null | undefined,
  now: number = Date.now(),
): string | null {
  if (!iso) return null;
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return null;
  const seconds = Math.round((now - then) / 1000);
  if (seconds < 0) return 'just now';
  if (seconds < 45) return 'just now';
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.round(hours / 24);
  if (days < 30) return `${days}d ago`;
  const months = Math.round(days / 30);
  if (months < 12) return `${months}mo ago`;
  return `${Math.round(months / 12)}y ago`;
}

/** Freshness window: a ready source last synced over this long ago reads as stale. */
export const STALE_AFTER_MS = 24 * 60 * 60 * 1000;

/** A freshness label + stale flag for a source's last successful sync. */
export function freshness(
  source: Source,
  now: number = Date.now(),
): { label: string; stale: boolean } | null {
  if (source.status === 'pending_auth') return { label: 'Awaiting consent', stale: false };
  if (source.status === 'pending') return { label: 'Not yet synced', stale: false };
  const rel = relativeTime(source.last_synced_at, now);
  if (!rel) return null;
  const then = source.last_synced_at ? Date.parse(source.last_synced_at) : NaN;
  const stale = !Number.isNaN(then) && now - then > STALE_AFTER_MS;
  return { label: `Synced ${rel}`, stale };
}

/**
 * ACL freshness for a managed source (ADR-0019 §2): when the mirrored ACLs were
 * last refreshed. Content whose ACL is older than the freshness window is
 * DENIED at retrieval, so staleness here is a trust signal, not cosmetics.
 * `null` acl_synced_at (before the first sync) renders as "not yet mirrored".
 */
export function aclFreshness(
  source: GdriveSource,
  now: number = Date.now(),
): { label: string; stale: boolean } {
  const rel = relativeTime(source.acl_synced_at, now);
  if (!rel) return { label: 'Permissions not yet mirrored', stale: false };
  const then = source.acl_synced_at ? Date.parse(source.acl_synced_at) : NaN;
  const stale = !Number.isNaN(then) && now - then > STALE_AFTER_MS;
  return { label: `Permissions mirrored ${rel}`, stale };
}

/** Client-side URL validation for the Add-source form (mirrors ADR-0009 §3 schemes). */
export interface UrlValidation {
  ok: boolean;
  /** A normalized URL to submit (trimmed) when `ok`. */
  url?: string;
  /** A user-facing reason when not `ok`. */
  error?: string;
}

/**
 * Validate a pasted URL BEFORE we POST: require a parseable absolute http(s) URL.
 * This is a UX guard, not a security boundary — the server runs the authoritative
 * SSRF check (ADR-0009 §3) and a blocked URL still comes back as a 422 we surface.
 */
export function validateUrl(raw: string): UrlValidation {
  const trimmed = raw.trim();
  if (!trimmed) return { ok: false, error: 'Paste a link to add a source.' };
  let parsed: URL;
  try {
    parsed = new URL(trimmed);
  } catch {
    return { ok: false, error: "That doesn't look like a valid URL. Include https://" };
  }
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
    return { ok: false, error: 'Only http:// and https:// links are supported.' };
  }
  if (!parsed.host) {
    return { ok: false, error: 'That URL is missing a host.' };
  }
  return { ok: true, url: trimmed };
}

/**
 * Map an api/ ApiError from `createSource` to an inline form message. A 422 is the
 * contract's catch-all for a malformed / SSRF-blocked URL or an invalid gdrive
 * config (INV-8): when the server tags a stable code we say so explicitly;
 * otherwise we surface the problem detail. A 403 is the admin gate on managed
 * sources (INV-5). 401 routes to re-auth elsewhere, so we keep it terse here.
 */
export function createSourceErrorMessage(error: ApiError): string {
  if (error.status === 422) {
    if (error.problem?.code === 'url_blocked') {
      return "That link can't be reached safely — it points to a blocked or private address.";
    }
    if (error.problem?.code === 'invalid_config') {
      return "That Drive configuration isn't valid. Check the folder or Shared Drive id.";
    }
    const fieldError = error.problem?.errors?.[0]?.message;
    return (
      fieldError ??
      error.problem?.detail ??
      "That link couldn't be added. Check the URL and try again."
    );
  }
  if (error.status === 403) {
    return 'Only a tenant admin can add a managed connector.';
  }
  if (error.status === 401) return 'Your session expired. Sign in again to add a source.';
  return error.displayMessage || "Couldn't add that source. Please try again.";
}

/**
 * Map an api/ ApiError from `deleteSource` to a human message the confirm
 * dialog renders inline (the outcome is never silently discarded). 403 = the
 * admin gate on managed-source mutations, checked at action time against the
 * caller's CURRENT roles — a demoted admin's delete is denied (ADR-0019 §1,
 * INV-5); 404 = existence non-disclosure / already gone (INV-1/INV-2).
 */
export function deleteSourceErrorMessage(error: ApiError): string {
  if (error.status === 403) {
    return 'Only a tenant admin can remove a managed source — your role may have changed.';
  }
  if (error.status === 404)
    return 'That source no longer exists — it may already have been removed.';
  if (error.status === 401) return 'Your session expired. Sign in again to remove this source.';
  return error.displayMessage || "Couldn't remove this source. Please try again.";
}

/**
 * Map an api/ ApiError from `connectSource` to a human message. 403 = the
 * admin gate on managed-source mutations, checked at action time (ADR-0019 §1,
 * INV-5); 409 = the source's type has no OAuth flow or it isn't connectable
 * (INV-8); 404 = existence non-disclosure (INV-1).
 */
export function connectSourceErrorMessage(error: ApiError): string {
  if (error.status === 403) {
    return 'Only a tenant admin can connect a managed source.';
  }
  if (error.status === 409) {
    return "This source can't start a consent flow right now.";
  }
  if (error.status === 404) return 'That source no longer exists.';
  if (error.status === 401) return 'Your session expired. Sign in again to connect this source.';
  return error.displayMessage || "Couldn't start the Google consent flow. Please try again.";
}

/** The closed reason set of the callback's error redirect (contracts §oauthCallback). */
export const CONNECT_ERROR_REASONS: readonly ConnectErrorReason[] = [
  'expired',
  'denied',
  'provider_error',
  'failed',
] as const;

/** Narrow an untrusted query-param string to the closed reason set. */
export function parseConnectErrorReason(raw: string | null): ConnectErrorReason | null {
  return (CONNECT_ERROR_REASONS as readonly string[]).includes(raw ?? '')
    ? (raw as ConnectErrorReason)
    : null;
}

/**
 * Human message for each closed `connect=error` reason code (the frozen redirect
 * contract). An unknown/missing reason falls back to the generic `failed` text —
 * never a blank banner.
 */
export function connectReturnErrorMessage(reason: ConnectErrorReason | null): string {
  switch (reason) {
    case 'expired':
      return 'The consent link expired or was already used. Start the connection again from the source card.';
    case 'denied':
      return 'The connection was not authorized — your admin role, or the source, may have changed. Check with a tenant admin and try again.';
    case 'provider_error':
      return 'Google reported a problem during consent (or the consent was cancelled). Try connecting again.';
    case 'failed':
    default:
      return "Something went wrong completing the connection. Try again — if it keeps failing, check the source's health.";
  }
}
