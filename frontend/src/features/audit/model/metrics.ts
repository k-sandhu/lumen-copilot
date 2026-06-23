/**
 * Client-side analytics for the audit screen (#121). Pure functions over the
 * page of events the api/ boundary already returned — NO extra backend calls,
 * NO invented data. The frozen `GET /audit` contract serves a cursor page of
 * events; everything here is derived honestly from THAT page, so the KPIs read
 * "this page" rather than pretending to a tenant-wide rollup the MVP backend
 * cannot serve (issue #121: "omit any metric with no data rather than faking
 * it"). Latency is deliberately absent — the wire `AuditEvent` carries no
 * latency field, so the wireframe's "Avg latency" tile is omitted, not faked.
 */
import type { AuditEvent } from '@/api';
import { kindForEventType } from './presentation';

/** The four kit kinds plus an "all" pseudo-segment for the segmented filter. */
export type AuditSegment = 'all' | 'retrieval' | 'answer' | 'action' | 'access';

/** Headline metrics derived purely from the events on the current page. */
export interface AuditMetrics {
  /** Events on this page (the unit the cursor contract can honestly serve). */
  total: number;
  /** Events whose decision was `denied` (access-denied count). */
  denied: number;
  /** `answer.generated` events on this page. */
  answers: number;
  /** Answer events that grounded on at least one allowed candidate. */
  answersCited: number;
  /**
   * Share of answers that cited a source, 0–1. `null` when there are no answer
   * events on the page — the tile then renders an em-dash rather than "0%",
   * so an empty page never reads as a grounding failure.
   */
  citedRate: number | null;
}

/** True when the event grounded on at least one allowed retrieval candidate. */
function isCited(event: AuditEvent): boolean {
  return event.provenance.candidates.some((c) => c.disposition === 'allow');
}

/** Derive the headline metrics from the page of events (pure, no I/O). */
export function summarizeEvents(events: readonly AuditEvent[]): AuditMetrics {
  let denied = 0;
  let answers = 0;
  let answersCited = 0;
  for (const e of events) {
    if (e.decision === 'denied') denied += 1;
    if (e.event_type === 'answer.generated') {
      answers += 1;
      if (isCited(e)) answersCited += 1;
    }
  }
  return {
    total: events.length,
    denied,
    answers,
    answersCited,
    citedRate: answers > 0 ? answersCited / answers : null,
  };
}

/** Format a 0–1 rate as a whole-ish percent, or em-dash when there's no data. */
export function formatRate(rate: number | null): string {
  if (rate === null) return '—';
  return `${(rate * 100).toFixed(rate === 1 ? 0 : 1)}%`;
}

/** Map a wire event_type to the segment it belongs to (reuses the kit fold). */
export function segmentForEvent(event: AuditEvent): Exclude<AuditSegment, 'all'> {
  return kindForEventType(event.event_type);
}

/**
 * Filter a page of events to one segment, CLIENT-SIDE over the already-fetched
 * page (issue #121). "Access denied" is the access-decision fold AND a denied
 * decision, so the segment matches the wireframe's "Access denied" chip rather
 * than every login/view that also folds to `access`.
 */
export function filterBySegment(
  events: readonly AuditEvent[],
  segment: AuditSegment,
): AuditEvent[] {
  if (segment === 'all') return [...events];
  if (segment === 'access') {
    return events.filter((e) => e.decision === 'denied');
  }
  return events.filter((e) => segmentForEvent(e) === segment);
}

/** Escape one CSV field per RFC 4180 — quote when it holds a comma/quote/newline. */
function csvField(value: string): string {
  if (/[",\r\n]/.test(value)) {
    return `"${value.replace(/"/g, '""')}"`;
  }
  return value;
}

const CSV_HEADER = [
  'id',
  'ts',
  'actor',
  'tenant_id',
  'event_type',
  'resource_id',
  'decision',
  'candidates_allowed',
  'candidates_excluded',
] as const;

/** A page of events → a flat RFC-4180 CSV string (the rows visible on screen). */
export function eventsToCsv(events: readonly AuditEvent[]): string {
  const rows = events.map((e) => {
    let allowed = 0;
    let excluded = 0;
    for (const c of e.provenance.candidates) {
      if (c.disposition === 'allow') allowed += 1;
      else excluded += 1;
    }
    return [
      e.id,
      e.ts,
      e.actor,
      e.tenant_id,
      e.event_type,
      e.resource_id ?? '',
      e.decision,
      String(allowed),
      String(excluded),
    ]
      .map(csvField)
      .join(',');
  });
  return [CSV_HEADER.join(','), ...rows].join('\r\n');
}
