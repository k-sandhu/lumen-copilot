/**
 * Typed health/readiness calls — part of the api/ boundary.
 *
 * `/health` and `/health/ready` are SAME-ORIGIN and live OUTSIDE `/api` (the
 * Vite proxy forwards `/health` directly to the backend). We pass absolute
 * paths so the client does not prefix them with VITE_API_BASE_URL.
 */
import { request } from './client';
import type { HealthStatus, ReadinessStatus } from './types';

/** Liveness — the process is up. */
export function getHealth(signal?: AbortSignal): Promise<HealthStatus> {
  return request<HealthStatus>('/health', { signal });
}

/**
 * Readiness — process + dependencies. The contract returns ReadinessStatus on
 * BOTH 200 (ready) and 503 (degraded), so we accept 503 and let the caller read
 * `status` to decide. A thrown ApiError here means a real transport failure.
 */
export function getReadiness(signal?: AbortSignal): Promise<ReadinessStatus> {
  return request<ReadinessStatus>('/health/ready', { signal, okStatuses: [503] });
}
