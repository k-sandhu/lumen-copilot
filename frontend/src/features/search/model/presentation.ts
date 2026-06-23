/**
 * Pure presentation helpers for the search slice (#84). No transport, no React —
 * just total functions that map the FROZEN wire shapes
 * (contracts/openapi.yaml §search; mirrored in api/types.ts) onto the trust-kit
 * component props. Kept pure so each mapping is unit-tested in isolation.
 */
import type { MatchSpan, PermissionState, SearchResult } from '@/api';
import type { PermissionLevel, SourcePassage } from '@/ui';

/**
 * Map the wire `permission` (allowed | restricted) onto the kit's
 * {@link PermissionLevel}. The wire never returns a fully-hidden result — those
 * are counted in `hidden_count`, not listed — so `denied` is unreachable here;
 * the trim notice surfaces hidden results instead (spec 0004 INV-2).
 */
export function toPermissionLevel(permission: PermissionState): PermissionLevel {
  return permission === 'restricted' ? 'restricted' : 'granted';
}

/** Human label for the permission pill, distinct per state. */
export function permissionLabel(permission: PermissionState): string {
  return permission === 'restricted' ? 'Restricted · content withheld' : 'You have access';
}

/**
 * Split a snippet into highlighted / plain runs from the matched character
 * spans, for `<mark>`-based highlighting. Spans are clamped to the snippet
 * bounds, sorted, and overlaps merged so the runs always tile the string exactly
 * once (no dropped or duplicated characters) regardless of the server's ordering.
 */
export function toPassageRuns(snippet: string, spans: MatchSpan[]): SourcePassage {
  const len = snippet.length;
  const clean = spans
    .map((s) => ({
      start: Math.max(0, Math.min(s.start, len)),
      end: Math.max(0, Math.min(s.end, len)),
    }))
    .filter((s) => s.end > s.start)
    .sort((a, b) => a.start - b.start);

  // Merge overlapping / adjacent spans so runs never double-count a character.
  const merged: MatchSpan[] = [];
  for (const span of clean) {
    const last = merged[merged.length - 1];
    if (last && span.start <= last.end) {
      last.end = Math.max(last.end, span.end);
    } else {
      merged.push({ ...span });
    }
  }

  if (merged.length === 0) {
    return { runs: snippet ? [{ text: snippet }] : [] };
  }

  const runs: SourcePassage['runs'] = [];
  let cursor = 0;
  for (const span of merged) {
    if (span.start > cursor) runs.push({ text: snippet.slice(cursor, span.start) });
    runs.push({ text: snippet.slice(span.start, span.end), highlight: true });
    cursor = span.end;
  }
  if (cursor < len) runs.push({ text: snippet.slice(cursor) });
  return { runs };
}

/** Milliseconds in each coarse bucket, largest first, for relative formatting. */
const UNITS: Array<{ ms: number; one: string; many: (n: number) => string }> = [
  { ms: 365 * 24 * 3600_000, one: 'a year', many: (n) => `${n} years` },
  { ms: 30 * 24 * 3600_000, one: 'a month', many: (n) => `${n} months` },
  { ms: 7 * 24 * 3600_000, one: 'a week', many: (n) => `${n} weeks` },
  { ms: 24 * 3600_000, one: 'a day', many: (n) => `${n} days` },
  { ms: 3600_000, one: 'an hour', many: (n) => `${n} hours` },
  { ms: 60_000, one: 'a minute', many: (n) => `${n} minutes` },
];

/**
 * "Indexed 2 hours ago" style freshness label from an ISO timestamp. Returns
 * `Indexed just now` under a minute, and degrades gracefully (`Indexed recently`)
 * if the timestamp is unparseable — never throws, never shows a raw date string.
 */
export function freshnessLabel(isoTimestamp: string, now: number = Date.now()): string {
  const t = Date.parse(isoTimestamp);
  if (Number.isNaN(t)) return 'Indexed recently';
  const delta = now - t;
  if (delta < 60_000) return 'Indexed just now';
  for (const unit of UNITS) {
    if (delta >= unit.ms) {
      const n = Math.floor(delta / unit.ms);
      return `Indexed ${n === 1 ? unit.one : unit.many(n)} ago`;
    }
  }
  return 'Indexed just now';
}

/** Freshness window past which a result is flagged stale (amber). 90 days. */
export const STALE_AFTER_MS = 90 * 24 * 3600_000;

/** Whether a result is past its freshness window (drives the amber pill). */
export function isStale(isoTimestamp: string, now: number = Date.now()): boolean {
  const t = Date.parse(isoTimestamp);
  if (Number.isNaN(t)) return false;
  return now - t >= STALE_AFTER_MS;
}

/** Emoji glyph per source kind, for the result-row affordance. */
export function sourceGlyph(source: SearchResult['source']): string {
  switch (source) {
    case 'upload':
      return '📄';
    case 'chat':
      return '💬';
    case 'connector':
      return '🔌';
    default:
      return '📄';
  }
}

/**
 * The trim-notice copy from `hidden_count`. Returns null when nothing was hidden
 * (so the notice is not rendered), else the exact "N results hidden — you don't
 * have access" disclosure (spec 0004 INV-2) — singular/plural aware.
 */
export function trimNotice(hiddenCount: number): string | null {
  if (hiddenCount <= 0) return null;
  const noun = hiddenCount === 1 ? 'result' : 'results';
  return `${hiddenCount} ${noun} hidden — you don't have access`;
}
