/**
 * Typed admin-console calls — part of the api/ boundary (ADR-0004). The ONLY
 * place the SPA performs admin HTTP. Conforms to the FROZEN contract
 * (contracts/openapi.yaml §admin, M2 #80). All three are READ-ONLY governance
 * surfaces restricted to the `admin` role; any other role receives 403 (INV-5),
 * and all are tenant-scoped (INV-1):
 *
 *   GET /admin/members          ?cursor&limit → MemberList
 *   GET /admin/model-governance               → ModelGovernance
 *   GET /admin/risk-tiers                      → RiskTierList
 *
 * Negative paths surface as typed `ApiError`s: 401 (INV-4) for a missing/expired
 * token, 403 (INV-5) for a non-admin caller.
 */
import { request } from './client';
import type { MemberList, ModelGovernance, RiskTierList } from './types';

export interface PageQuery {
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

/** The tenant's members and their roles (admin only). */
export function listMembers(page: PageQuery = {}, signal?: AbortSignal): Promise<MemberList> {
  return request<MemberList>(`/admin/members${buildQuery({ ...page })}`, { signal });
}

/** Which models are allowed, by governance tier (admin only). */
export function getModelGovernance(signal?: AbortSignal): Promise<ModelGovernance> {
  return request<ModelGovernance>('/admin/model-governance', { signal });
}

/** The read-before-write risk tiers T0–T3 (admin only). */
export function getRiskTiers(signal?: AbortSignal): Promise<RiskTierList> {
  return request<RiskTierList>('/admin/risk-tiers', { signal });
}
