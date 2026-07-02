/**
 * Cadence + timezone helpers for the schedule form (#237) — pure, unit-testable,
 * kept out of the JSX (frontend/AGENTS.md: no business logic in JSX).
 *
 * The wire `Cadence` is a oneOf: either `{ cron }` OR `{ structured }`
 * (StructuredCadence). The form lets the user pick a friendly recurrence (every
 * hour/day/week/month at a local time) OR type a raw 5-field cron expression. This
 * module owns:
 *   - client-side VALIDATION with friendly messages (AC-4) — the server is still
 *     authoritative (a bad cron/timezone → 422, INV-8), but we catch obvious
 *     mistakes before the round-trip and message the 422 honestly;
 *   - conversion between the form draft and the wire `Cadence`;
 *   - a human-readable one-line summary for the list + review.
 *
 * Timezone validation uses the platform `Intl` API (Intl.supportedValuesOf when
 * available; otherwise a probe via `Intl.DateTimeFormat`), so we never ship a
 * hand-maintained IANA list that drifts.
 */
import type { Cadence, CadenceUnit, StructuredCadence } from '@/api';

/** How the user is expressing the recurrence in the form. */
export type CadenceMode = 'structured' | 'cron';

/** The form's editable cadence draft (both modes held so switching is lossless). */
export interface CadenceDraft {
  mode: CadenceMode;
  /** Structured recurrence period. */
  every: CadenceUnit;
  /** Local time-of-day HH:MM (24h). */
  at: string;
  /** 0=Sunday..6=Saturday (used when every==='week'). */
  dayOfWeek: number;
  /** 1..31 (used when every==='month'). */
  dayOfMonth: number;
  /** Raw 5-field cron expression (used in cron mode). */
  cron: string;
}

/** A fresh cadence draft: every day at 08:00 local. */
export function emptyCadence(): CadenceDraft {
  return {
    mode: 'structured',
    every: 'day',
    at: '08:00',
    dayOfWeek: 1,
    dayOfMonth: 1,
    cron: '0 8 * * 1',
  };
}

const HHMM = /^([01][0-9]|2[0-3]):[0-5][0-9]$/;
export const CADENCE_UNITS: CadenceUnit[] = ['hour', 'day', 'week', 'month'];
export const WEEKDAYS = [
  'Sunday',
  'Monday',
  'Tuesday',
  'Wednesday',
  'Thursday',
  'Friday',
  'Saturday',
] as const;

/**
 * Validate a raw 5-field cron expression enough to catch obvious mistakes with a
 * friendly message. The server (croniter) is authoritative — this is a fast local
 * guard, not a full cron parser. Returns null when the expression looks well-formed.
 */
export function validateCron(expr: string): string | null {
  const trimmed = expr.trim();
  if (trimmed.length === 0) return 'Enter a cron expression, e.g. “0 8 * * 1”.';
  const fields = trimmed.split(/\s+/);
  if (fields.length !== 5) {
    return `A cron expression has 5 fields (minute hour day month weekday) — got ${fields.length}.`;
  }
  // Each field: a number, *, list, range, or step of the allowed characters.
  const FIELD = /^[*0-9,\-/]+$/;
  const names = ['minute', 'hour', 'day-of-month', 'month', 'day-of-week'];
  for (let i = 0; i < fields.length; i += 1) {
    if (!FIELD.test(fields[i] ?? '')) {
      return `The ${names[i]} field “${fields[i]}” isn’t valid — use numbers, *, ranges (1-5), lists (1,3), or steps (*/2).`;
    }
  }
  return null;
}

/** Validate the local time-of-day (structured mode). Null when valid. */
export function validateTimeOfDay(at: string): string | null {
  if (!HHMM.test(at.trim())) return 'Enter a time as HH:MM (24-hour), e.g. 08:00.';
  return null;
}

/**
 * The IANA timezone names the platform knows. Uses `Intl.supportedValuesOf` where
 * available (modern browsers / Node ≥18); returns [] if unsupported so callers
 * fall back to the probe validator.
 */
export function knownTimezones(): string[] {
  const intl = Intl as unknown as { supportedValuesOf?: (k: string) => string[] };
  try {
    return intl.supportedValuesOf ? intl.supportedValuesOf('timeZone') : [];
  } catch {
    return [];
  }
}

/**
 * Validate an IANA timezone name. Probes `Intl.DateTimeFormat`, which throws a
 * RangeError for an unknown zone — so we validate against the actual platform DB,
 * mirroring the server's `zoneinfo` check (a bad name → 422 `invalid_timezone`).
 * Null when valid.
 */
export function validateTimezone(tz: string): string | null {
  const trimmed = tz.trim();
  if (trimmed.length === 0) return 'Pick a timezone — it drives when the schedule fires.';
  try {
    // Throws RangeError('Invalid time zone specified') for an unknown name.
    new Intl.DateTimeFormat('en-US', { timeZone: trimmed });
    return null;
  } catch {
    return `“${trimmed}” isn’t a known IANA timezone (e.g. America/New_York, Europe/London).`;
  }
}

/** The caller's current IANA timezone, for a sensible form default. */
export function localTimezone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
  } catch {
    return 'UTC';
  }
}

/** Whether the whole cadence draft is valid (drives the submit gate). */
export function validateCadence(draft: CadenceDraft): string | null {
  if (draft.mode === 'cron') return validateCron(draft.cron);
  return validateTimeOfDay(draft.at);
}

/** Convert a valid cadence draft to the wire `Cadence` (throws if invalid). */
export function draftToCadence(draft: CadenceDraft): Cadence {
  if (draft.mode === 'cron') {
    const err = validateCron(draft.cron);
    if (err) throw new Error(err);
    return { cron: draft.cron.trim() };
  }
  const err = validateTimeOfDay(draft.at);
  if (err) throw new Error(err);
  const structured: StructuredCadence = { every: draft.every, at: draft.at.trim() };
  if (draft.every === 'week') structured.day_of_week = draft.dayOfWeek;
  if (draft.every === 'month') structured.day_of_month = draft.dayOfMonth;
  return { structured };
}

/** Hydrate a cadence draft from a wire `Cadence` (edit mode). */
export function cadenceToDraft(cadence: Cadence): CadenceDraft {
  const base = emptyCadence();
  if ('cron' in cadence) {
    return { ...base, mode: 'cron', cron: cadence.cron };
  }
  const s = cadence.structured;
  return {
    ...base,
    mode: 'structured',
    every: s.every,
    at: s.at,
    dayOfWeek: s.day_of_week ?? base.dayOfWeek,
    dayOfMonth: s.day_of_month ?? base.dayOfMonth,
  };
}

/** A human one-line summary of a cadence for the list + review (e.g. "Every day at 08:00"). */
export function describeCadence(cadence: Cadence): string {
  if ('cron' in cadence) return `cron: ${cadence.cron}`;
  const s = cadence.structured;
  switch (s.every) {
    case 'hour':
      return `Every hour at :${s.at.slice(3)}`;
    case 'day':
      return `Every day at ${s.at}`;
    case 'week':
      return `Every ${WEEKDAYS[s.day_of_week ?? 0]} at ${s.at}`;
    case 'month':
      return `Monthly on day ${s.day_of_month ?? 1} at ${s.at}`;
    default:
      return s.at;
  }
}
