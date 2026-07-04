/**
 * Typed run-delivery calls — part of the api/ boundary (ADR-0004). The ONLY place
 * the SPA performs run-delivery HTTP. Conforms to the FROZEN contract
 * (contracts/openapi.yaml §run-deliveries, ADR-0015 / #238):
 *
 *   GET  /run-deliveries              ?status&unread&cursor&limit → RunDeliveryList
 *   POST /run-deliveries/{id}/read                                → RunDelivery
 *
 * The list is the in-app run inbox — a completed run's `summary` + a `run_id` link
 * to the full cited transcript. Deliveries are tenant- and owner-scoped (spec 0004
 * INV-1/INV-2): a caller only ever sees their own. Negative paths surface as typed
 * `ApiError`s the UI branches on: a missing/expired token → 401 (INV-4); a bad
 * filter value → 422 (INV-8); a non-owned / cross-tenant / unknown id → 404
 * (existence non-disclosure). External channels are deferred — this is in-app only.
 */
import { request } from './client';
import type { RunDelivery, RunDeliveryList, RunDeliveryStatus } from './types';

/** List filters + cursor page params for GET /run-deliveries. */
export interface RunDeliveryPageQuery {
  /** Filter to one delivery status. */
  status?: RunDeliveryStatus;
  /** When true, exclude already-read deliveries (the unread inbox). */
  unread?: boolean;
  cursor?: string;
  limit?: number;
}

function buildQuery(params: Record<string, string | number | boolean | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== '' && value !== false) search.set(key, String(value));
  }
  const qs = search.toString();
  return qs ? `?${qs}` : '';
}

/** List the caller's run deliveries (owner-scoped), one cursor page, newest first. */
export function listRunDeliveries(
  query: RunDeliveryPageQuery = {},
  signal?: AbortSignal,
): Promise<RunDeliveryList> {
  return request<RunDeliveryList>(`/run-deliveries${buildQuery({ ...query })}`, { signal });
}

/**
 * Mark one delivery read (idempotent). Returns the updated (read) delivery. A
 * non-owned / cross-tenant / unknown id → 404 (INV-1/INV-2).
 */
export function markRunDeliveryRead(id: string): Promise<RunDelivery> {
  return request<RunDelivery>(`/run-deliveries/${id}/read`, { method: 'POST' });
}
