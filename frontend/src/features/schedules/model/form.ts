/**
 * Form model for the schedule editor (#237) — the client-side draft and the pure
 * mappers between it and the wire shapes (ScheduleCreate / ScheduleUpdate). Kept
 * out of the component so it is unit-testable and the JSX stays presentational
 * (frontend/AGENTS.md: no business logic in JSX).
 *
 * `input_params` and `delivery` are the assistant inputs + where a completed run
 * lands (in-app inbox + optional digest — v1 delivery is in-app only, ADR-0015 §6;
 * external channels are out of scope for #237, F-SCHED-4).
 */
import type {
  Schedule,
  ScheduleCreate,
  ScheduleDigest,
  ScheduleUpdate,
  OverlapPolicy,
} from '@/api';
import {
  cadenceToDraft,
  draftToCadence,
  emptyCadence,
  localTimezone,
  validateCadence,
  validateTimezone,
  type CadenceDraft,
} from './cadence';

/** One input-param row (JSON object is edited as key/value pairs in the UI). */
export interface ParamRow {
  key: string;
  value: string;
}

/** The editable draft the schedule form binds its inputs to. */
export interface ScheduleFormState {
  /** The saved assistant this schedule runs ('' ⇒ not yet picked). */
  assistantId: string;
  cadence: CadenceDraft;
  /** IANA timezone name. */
  timezone: string;
  /** Assistant inputs as key/value rows (mapped to the input_params object). */
  params: ParamRow[];
  /** Land completed runs in the in-app inbox. */
  deliverInbox: boolean;
  /** Optional in-app digest cadence. */
  digest: Exclude<ScheduleDigest, null>;
  overlapPolicy: OverlapPolicy;
  /** Create firing (true) or paused (false). */
  enabled: boolean;
}

export const OVERLAP_OPTIONS: Array<{ value: OverlapPolicy; label: string; hint: string }> = [
  { value: 'skip', label: 'Skip', hint: 'If the previous run is still going, skip this fire.' },
  { value: 'queue', label: 'Queue', hint: 'Run after the active run finishes.' },
  { value: 'allow', label: 'Allow', hint: 'Run concurrently with the active run.' },
];

export const DIGEST_OPTIONS: Array<{ value: Exclude<ScheduleDigest, null>; label: string }> = [
  { value: 'none', label: 'No digest' },
  { value: 'daily', label: 'Daily digest' },
  { value: 'weekly', label: 'Weekly digest' },
];

/** A fresh draft for `/schedules/new`. */
export function emptyForm(assistantId = ''): ScheduleFormState {
  return {
    assistantId,
    cadence: emptyCadence(),
    timezone: localTimezone(),
    params: [],
    deliverInbox: true,
    digest: 'none',
    overlapPolicy: 'skip',
    enabled: true,
  };
}

/** Hydrate the form from an existing schedule (edit mode). */
export function formFromSchedule(s: Schedule): ScheduleFormState {
  return {
    assistantId: s.assistant_id,
    cadence: cadenceToDraft(s.cadence),
    timezone: s.timezone,
    params: paramsToRows(s.input_params),
    deliverInbox: s.delivery.inbox,
    digest: s.delivery.digest ?? 'none',
    overlapPolicy: s.overlap_policy,
    enabled: s.enabled,
  };
}

/** Convert a wire input_params object to editable key/value rows. */
export function paramsToRows(params: Record<string, unknown> | undefined): ParamRow[] {
  if (!params) return [];
  return Object.entries(params).map(([key, value]) => ({
    key,
    value: typeof value === 'string' ? value : JSON.stringify(value),
  }));
}

/** Convert editable rows back to an input_params object (drops empty keys). */
export function rowsToParams(rows: ParamRow[]): Record<string, unknown> | undefined {
  const entries = rows
    .map((r) => [r.key.trim(), r.value] as const)
    .filter(([key]) => key.length > 0);
  if (entries.length === 0) return undefined;
  return Object.fromEntries(entries);
}

/** Field-level validation results, keyed by the form field, empty when clean. */
export interface ScheduleFormErrors {
  assistantId?: string;
  cadence?: string;
  timezone?: string;
}

/** Validate the form for submit (AC-4: friendly cron/timezone messages). */
export function validateForm(form: ScheduleFormState): ScheduleFormErrors {
  const errors: ScheduleFormErrors = {};
  if (form.assistantId.trim().length === 0) {
    errors.assistantId = 'Pick the assistant this schedule should run.';
  }
  const cadenceErr = validateCadence(form.cadence);
  if (cadenceErr) errors.cadence = cadenceErr;
  const tzErr = validateTimezone(form.timezone);
  if (tzErr) errors.timezone = tzErr;
  return errors;
}

export function isValid(errors: ScheduleFormErrors): boolean {
  return !errors.assistantId && !errors.cadence && !errors.timezone;
}

/** The create body (POST /schedules). Throws if the cadence draft is invalid. */
export function toCreateBody(form: ScheduleFormState): ScheduleCreate {
  const body: ScheduleCreate = {
    assistant_id: form.assistantId,
    cadence: draftToCadence(form.cadence),
    timezone: form.timezone.trim(),
    delivery: { inbox: form.deliverInbox, digest: form.digest },
    overlap_policy: form.overlapPolicy,
    enabled: form.enabled,
  };
  const params = rowsToParams(form.params);
  if (params) body.input_params = params;
  return body;
}

/** The update body (PATCH /schedules/{id}) — sends the whole editable head. */
export function toUpdateBody(form: ScheduleFormState): ScheduleUpdate {
  return {
    cadence: draftToCadence(form.cadence),
    timezone: form.timezone.trim(),
    input_params: rowsToParams(form.params) ?? {},
    delivery: { inbox: form.deliverInbox, digest: form.digest },
    overlap_policy: form.overlapPolicy,
    enabled: form.enabled,
  };
}
