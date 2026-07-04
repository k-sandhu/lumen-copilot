/**
 * Typed run calls — part of the api/ boundary (ADR-0004). The ONLY place the SPA
 * performs run HTTP. Conforms to the FROZEN contract (contracts/openapi.yaml
 * §runs, ADR-0015 / #234):
 *
 *   GET  /runs           ?assistant_id&schedule_id&status&cursor&limit → RunList
 *   GET  /runs/{id}                                                    → Run
 *   POST /runs/{id}/resume                                             → Run (202)
 *   POST /runs/{id}/cancel                                             → Run (200)
 *   POST /runs/{id}/reroute   {to_owner_id}                            → Run (202)
 *
 * The list is the run inbox — status/trigger/timestamps + the `summary` digest
 * line per item (transcript, tool calls and citations live on the detail). Runs
 * are tenant- and owner-scoped (spec 0004 INV-1/INV-2): a caller only ever sees
 * their own runs. Negative paths surface as typed `ApiError`s the UI branches on:
 * a missing/expired token → 401 (INV-4); a bad filter value → 422 (INV-8); a
 * non-owned / cross-tenant / unknown id → 404 (existence non-disclosure).
 */
import { request } from './client';
import type { Run, RunList, RunReroute, RunStatus } from './types';

/** List filters + cursor page params for GET /runs. */
export interface RunPageQuery {
  /** Filter to runs of one assistant. */
  assistant_id?: string;
  /** Filter to runs from one schedule (omit for both scheduled + manual). */
  schedule_id?: string;
  /** Filter to one run status. */
  status?: RunStatus;
  cursor?: string;
  limit?: number;
}

function buildQuery(params: Record<string, string | number | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== '') search.set(key, String(value));
  }
  const qs = search.toString();
  return qs ? `?${qs}` : '';
}

/** List the caller's runs (owner-scoped), one cursor page, newest first. */
export function listRuns(query: RunPageQuery = {}, signal?: AbortSignal): Promise<RunList> {
  return request<RunList>(`/runs${buildQuery({ ...query })}`, { signal });
}

/**
 * Run detail — status, inputs, transcript (`steps`), citations, outputs, and the
 * typed error / escalation reason. A non-owned / cross-tenant / unknown id → 404
 * (INV-1/INV-2).
 */
export function getRun(id: string, signal?: AbortSignal): Promise<Run> {
  return request<Run>(`/runs/${id}`, { signal });
}

/**
 * Resume an **escalated** run (E7-5, #239) — re-enqueue it from the escalation
 * point; returns the run reset to `queued`. Not escalated → 409 (INV-8); a
 * non-owned / cross-tenant id → 404 (INV-1/INV-2).
 */
export function resumeRun(id: string): Promise<Run> {
  return request<Run>(`/runs/${id}/resume`, { method: 'POST', okStatuses: [202] });
}

/**
 * Cancel an **escalated** run (E7-5) — acknowledge + close it to a permanent
 * terminal (a `cancelled` reason in `error`); returns the closed run. Not escalated
 * → 409; a non-owned / cross-tenant id → 404.
 */
export function cancelRun(id: string): Promise<Run> {
  return request<Run>(`/runs/${id}/cancel`, { method: 'POST' });
}

/**
 * Reroute an **escalated** run to another owner (E7-5) — reassign its execution
 * principal and re-enqueue it as the new owner (INV-2, never widening access);
 * returns the run reset to `queued`. The target must be a user in the tenant (else
 * 404) and not the current owner (else 422). Not escalated → 409.
 */
export function rerouteRun(id: string, body: RunReroute): Promise<Run> {
  return request<Run>(`/runs/${id}/reroute`, { method: 'POST', json: body, okStatuses: [202] });
}
