/**
 * Typed schedule calls — part of the api/ boundary (ADR-0004). The ONLY place the
 * SPA performs schedule HTTP. Conforms to the FROZEN contract
 * (contracts/openapi.yaml §schedules, ADR-0015 / #234):
 *
 *   GET    /schedules                       ?assistant_id&enabled&cursor&limit → ScheduleList
 *   POST   /schedules                       {assistant_id, cadence, timezone, …} → Schedule (201)
 *   GET    /schedules/{id}                                                       → Schedule
 *   PATCH  /schedules/{id}                  {…}                                  → Schedule
 *   DELETE /schedules/{id}                                                       → 204
 *   POST   /schedules/{id}/pause                                                 → Schedule (200)
 *   POST   /schedules/{id}/resume                                               → Schedule (200)
 *   POST   /schedules/{id}/run-now                                              → RunEnqueued (202)
 *
 * Schedules are tenant- and owner-scoped (spec 0004 INV-1/INV-2): a caller only
 * sees / mutates their own schedules within their tenant. Negative paths surface
 * as typed `ApiError`s the UI branches on (problem body, not status text): a
 * missing/expired token → 401 (INV-4); a malformed cron / IANA timezone → 422
 * (INV-8, `code` = `invalid_cron` / `invalid_timezone`); an unknown assistant →
 * 404 on create; a non-owned / cross-tenant / unknown id → 404 (existence
 * non-disclosure, INV-1/INV-2); run-now against an unavailable assistant / illegal
 * state → 409 (INV-8).
 */
import { request } from './client';
import type { RunEnqueued, Schedule, ScheduleCreate, ScheduleList, ScheduleUpdate } from './types';

/** List filters + cursor page params for GET /schedules. */
export interface SchedulePageQuery {
  /** Filter to schedules that run one assistant. */
  assistant_id?: string;
  /** Filter to enabled (true) or paused (false) schedules. */
  enabled?: boolean;
  cursor?: string;
  limit?: number;
}

function buildQuery(params: Record<string, string | number | boolean | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== '') search.set(key, String(value));
  }
  const qs = search.toString();
  return qs ? `?${qs}` : '';
}

/** List the caller's schedules (owner-scoped), one cursor page, newest first. */
export function listSchedules(
  query: SchedulePageQuery = {},
  signal?: AbortSignal,
): Promise<ScheduleList> {
  return request<ScheduleList>(`/schedules${buildQuery({ ...query })}`, { signal });
}

/**
 * Create a schedule (a recurring assistant run). A malformed cron / unknown IANA
 * timezone → 422 (INV-8), surfaced inline; an unknown assistant → 404.
 */
export function createSchedule(body: ScheduleCreate): Promise<Schedule> {
  return request<Schedule>('/schedules', { method: 'POST', json: body });
}

/** Get one schedule (404 if not yours / cross-tenant — INV-1/INV-2). */
export function getSchedule(id: string, signal?: AbortSignal): Promise<Schedule> {
  return request<Schedule>(`/schedules/${id}`, { signal });
}

/**
 * Edit a schedule (cadence, timezone, inputs, delivery, overlap). At least one
 * field must be present. A malformed cron / IANA timezone → 422; a non-owned /
 * cross-tenant id → 404.
 */
export function updateSchedule(id: string, body: ScheduleUpdate): Promise<Schedule> {
  return request<Schedule>(`/schedules/${id}`, { method: 'PATCH', json: body });
}

/** Delete a schedule (204; cascades its runs per retention). Non-owned id → 404. */
export function deleteSchedule(id: string): Promise<void> {
  return request<void>(`/schedules/${id}`, { method: 'DELETE' });
}

/**
 * Pause a schedule (enabled=false) — idempotent; returns the updated schedule with
 * `next_run_at` null. A non-owned / cross-tenant id → 404.
 */
export function pauseSchedule(id: string): Promise<Schedule> {
  return request<Schedule>(`/schedules/${id}/pause`, { method: 'POST' });
}

/**
 * Resume a schedule (enabled=true) — idempotent; returns the updated schedule with
 * `next_run_at` recomputed. A non-owned / cross-tenant id → 404.
 */
export function resumeSchedule(id: string): Promise<Schedule> {
  return request<Schedule>(`/schedules/${id}/resume`, { method: 'POST' });
}

/**
 * Enqueue an out-of-band manual run immediately (202). Returns the new run's id
 * (its WS streamId for live attach). A non-owned / cross-tenant id → 404; an
 * unavailable assistant / illegal state → 409 (INV-8).
 */
export function runScheduleNow(id: string): Promise<RunEnqueued> {
  return request<RunEnqueued>(`/schedules/${id}/run-now`, {
    method: 'POST',
    okStatuses: [202],
  });
}
