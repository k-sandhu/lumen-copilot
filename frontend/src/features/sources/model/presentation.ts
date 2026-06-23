/**
 * Pure presentation helpers for the Sources slice (#27, ADR-0009). Wire `Source`
 * shapes → the trust-signal vocabulary the screen renders (StatusDot tone +
 * label, freshness, the connector glyph, a human URL), plus the client-side URL
 * validation that gates the Add-source form before we ever POST.
 *
 * Kept side-effect-free and framework-free so they unit-test in isolation; the
 * api/ boundary is never touched here (no transport).
 */
import type { ApiError } from '@/api';
import type { Source, SourceStatus, StatusTone } from './types';

/** Map a sync status to a StatusDot tone (ADR-0009 §4 lifecycle → DESIGN §6). */
export function statusTone(status: SourceStatus): StatusTone {
  switch (status) {
    case 'ready':
      return 'ok';
    case 'syncing':
      return 'sync';
    case 'error':
      return 'danger';
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
    case 'pending':
    default:
      return { modifier: '', label: 'Pending' };
  }
}

/**
 * A 2-letter glyph for the connector card (the wireframe's `.src` tile). For a
 * `web` source we key off the URL host's first letters so cards stay visually
 * distinct without per-vendor branding (the web connector is vendor-neutral).
 */
export function sourceGlyph(source: Source): string {
  const host = safeHost(source.config.url);
  if (!host) return 'WB';
  const bare = host.replace(/^www\./, '');
  const letters = bare.replace(/[^a-z0-9]/gi, '');
  return (letters.slice(0, 2) || 'WB').toUpperCase();
}

/** The display name for a source card — the URL host, falling back to the raw URL. */
export function sourceName(source: Source): string {
  return safeHost(source.config.url) ?? source.config.url;
}

/** Human label for the web mode (page / feed / sitemap). */
export function modeLabel(source: Source): string {
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
export function relativeTime(iso: string | null | undefined, now: number = Date.now()): string | null {
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
  if (source.status === 'pending') return { label: 'Not yet synced', stale: false };
  const rel = relativeTime(source.last_synced_at, now);
  if (!rel) return null;
  const then = source.last_synced_at ? Date.parse(source.last_synced_at) : NaN;
  const stale = !Number.isNaN(then) && now - then > STALE_AFTER_MS;
  return { label: `Synced ${rel}`, stale };
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
 * contract's catch-all for a malformed / SSRF-blocked URL (ADR-0009 §3): when the
 * server tags it `url_blocked` we say so explicitly; otherwise we surface the
 * problem detail. 401 routes to re-auth elsewhere, so we keep it terse here.
 */
export function createSourceErrorMessage(error: ApiError): string {
  if (error.status === 422) {
    if (error.problem?.code === 'url_blocked') {
      return "That link can't be reached safely — it points to a blocked or private address.";
    }
    const fieldError = error.problem?.errors?.[0]?.message;
    return (
      fieldError ?? error.problem?.detail ?? "That link couldn't be added. Check the URL and try again."
    );
  }
  if (error.status === 401) return 'Your session expired. Sign in again to add a source.';
  return error.displayMessage || "Couldn't add that source. Please try again.";
}
