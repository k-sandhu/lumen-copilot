/**
 * Pure presentation helpers for the MCP-servers slice (#228, ADR-0012). Wire
 * `McpServer` / `McpTool` shapes → the trust-signal vocabulary the screen renders
 * (StatusDot tone + label, the transport label, a human endpoint host, the
 * risk-tier badge tier), plus the client-side endpoint validation that gates the
 * register form before we ever POST, and the ApiError → inline-message mapping.
 *
 * Kept side-effect-free and framework-free so they unit-test in isolation; the
 * api/ boundary is never touched here (no transport).
 */
import type { ApiError } from '@/api';
import type {
  McpRiskTier,
  McpServer,
  McpServerStatus,
  McpTool,
  McpTransport,
  StatusTone,
} from './types';

/** Map a health status to a StatusDot tone (ADR-0012 §5 lifecycle → DESIGN §6). */
export function statusTone(status: McpServerStatus): StatusTone {
  switch (status) {
    case 'ready':
      return 'ok';
    case 'error':
      return 'danger';
    case 'pending':
    default:
      return 'muted';
  }
}

/** A short status verb for the health dot label. */
export function statusLabel(status: McpServerStatus): string {
  switch (status) {
    case 'ready':
      return 'Healthy';
    case 'error':
      return 'Unreachable';
    case 'pending':
    default:
      return 'Not tested';
  }
}

/** Badge variant (lc-badge--*) mirroring the connected/ pending/ failed chips. */
export function statusBadge(status: McpServerStatus): { modifier: string; label: string } {
  switch (status) {
    case 'ready':
      return { modifier: 'lc-badge--ok', label: 'Ready' };
    case 'error':
      return { modifier: 'lc-badge--danger', label: 'Error' };
    case 'pending':
    default:
      return { modifier: '', label: 'Pending' };
  }
}

/** Human label for the remote transport (streamable_http | sse). */
export function transportLabel(transport: McpTransport): string {
  switch (transport) {
    case 'sse':
      return 'SSE';
    case 'streamable_http':
    default:
      return 'Streamable HTTP';
  }
}

/**
 * A 2-letter glyph for the server card — keyed off the endpoint host's first
 * letters so cards stay visually distinct without per-vendor branding.
 */
export function serverGlyph(server: McpServer): string {
  const host = safeHost(server.endpoint_url);
  if (!host) return 'MC';
  const bare = host.replace(/^www\./, '');
  const letters = bare.replace(/[^a-z0-9]/gi, '');
  return (letters.slice(0, 2) || 'MC').toUpperCase();
}

/** The endpoint host (no scheme), falling back to the raw URL when unparseable. */
export function endpointHost(server: McpServer): string {
  return safeHost(server.endpoint_url) ?? server.endpoint_url;
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

/** Human "last checked" label for the detail view / card health line. */
export function lastCheckedLabel(server: McpServer, now: number = Date.now()): string {
  if (server.status === 'pending') return 'Never tested';
  const rel = relativeTime(server.last_health_at, now);
  return rel ? `Checked ${rel}` : 'Not checked yet';
}

/** The RiskTierBadge tier for a discovered tool (the contract already emits T0–T3). */
export function toolTier(tool: McpTool): McpRiskTier {
  return tool.risk_tier;
}

/** Client-side endpoint validation for the register form (mirrors ADR-0012 §1/§4). */
export interface EndpointValidation {
  ok: boolean;
  /** A normalized URL to submit (trimmed) when `ok`. */
  url?: string;
  /** A user-facing reason when not `ok`. */
  error?: string;
}

/**
 * Validate a pasted endpoint URL BEFORE we POST: require a parseable absolute
 * https URL. This is a UX guard, not a security boundary — the server runs the
 * authoritative https + SSRF check (ADR-0012 §4) and a blocked / non-https URL
 * still comes back as a 422 we surface inline.
 */
export function validateEndpoint(raw: string): EndpointValidation {
  const trimmed = raw.trim();
  if (!trimmed) return { ok: false, error: 'Enter the server’s endpoint URL.' };
  let parsed: URL;
  try {
    parsed = new URL(trimmed);
  } catch {
    return { ok: false, error: "That doesn't look like a valid URL. Include https://" };
  }
  if (parsed.protocol !== 'https:') {
    return { ok: false, error: 'MCP endpoints must use https:// — plain http isn’t allowed.' };
  }
  if (!parsed.host) {
    return { ok: false, error: 'That URL is missing a host.' };
  }
  return { ok: true, url: trimmed };
}

/**
 * Map an api/ ApiError from `registerMcpServer` / `updateMcpServer` to an inline
 * form message. A 422 is the contract's catch-all for a malformed body, an
 * unsupported transport, or a non-https / SSRF-blocked endpoint (ADR-0012 §1/§4);
 * `code` distinguishes the cases (`endpoint_blocked`, `unsupported_transport`) so
 * we can say WHY the endpoint was refused rather than a generic failure.
 */
export function registerErrorMessage(error: ApiError): string {
  if (error.status === 422) {
    const code = error.problem?.code;
    if (code === 'endpoint_blocked') {
      return 'That endpoint can’t be reached safely — it points to a blocked, private, or non-public address.';
    }
    if (code === 'unsupported_transport') {
      return 'That transport isn’t supported — MCP servers must use Streamable HTTP or SSE (local/stdio servers aren’t allowed).';
    }
    const fieldError = error.problem?.errors?.[0]?.message;
    return (
      fieldError ??
      error.problem?.detail ??
      'That server couldn’t be registered. Check the endpoint and try again.'
    );
  }
  if (error.status === 401) return 'Your session expired. Sign in again to register a server.';
  return error.displayMessage || 'Couldn’t register that server. Please try again.';
}

/**
 * Map an ApiError from a `test` / `list-tools` / list read to a screen message.
 * A 404 on a server-scoped action is existence non-disclosure (INV-1/INV-2); a
 * 401 routes to re-auth. Everything else surfaces the problem detail.
 */
export function serverErrorMessage(error: ApiError): string {
  if (error.status === 401) return 'Your session expired. Sign in again to manage MCP servers.';
  if (error.status === 404) return 'That server no longer exists.';
  return error.displayMessage || 'Something went wrong. Please try again.';
}
