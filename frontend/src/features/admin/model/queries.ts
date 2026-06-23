/**
 * Server-state hooks for the admin slice — TanStack Query (#88). The three admin
 * governance surfaces are READ-ONLY server data (ADR-0007 §4): the member roster,
 * model governance, and the risk-tier reference. There are NO mutations here —
 * the admin screen is read-mostly for v1; write/governance actions are gated
 * behind mission filter #3 (read-before-write) and deferred.
 *
 * Conforms to the frozen contract (contracts/openapi.yaml §admin, #80) via the
 * api/ boundary only — no transport in this slice (frontend/AGENTS.md).
 *
 * All three are tenant-scoped (INV-1) and admin-only: a non-admin caller's 403
 * (INV-5) and a missing/expired token's 401 (INV-4) surface as the query's
 * `error` (a typed `ApiError`), which each panel renders as an actionable state.
 */
import { useQuery, type UseQueryResult } from '@tanstack/react-query';
import { getModelGovernance, getRiskTiers, listMembers } from '@/api';
import type { MemberList, ModelGovernance, RiskTierList } from '@/api';

export const membersQueryKey = ['admin', 'members'] as const;
export const modelGovernanceQueryKey = ['admin', 'model-governance'] as const;
export const riskTiersQueryKey = ['admin', 'risk-tiers'] as const;

/** The tenant's members and their roles (admin only). */
export function useMembers(): UseQueryResult<MemberList> {
  return useQuery<MemberList>({
    queryKey: membersQueryKey,
    queryFn: ({ signal }) => listMembers({}, signal),
    staleTime: 30_000,
  });
}

/** Which models are allowed, by governance tier (admin only). */
export function useModelGovernance(): UseQueryResult<ModelGovernance> {
  return useQuery<ModelGovernance>({
    queryKey: modelGovernanceQueryKey,
    queryFn: ({ signal }) => getModelGovernance(signal),
    staleTime: 30_000,
  });
}

/** The read-before-write risk tiers T0–T3 (admin only). */
export function useRiskTiers(): UseQueryResult<RiskTierList> {
  return useQuery<RiskTierList>({
    queryKey: riskTiersQueryKey,
    queryFn: ({ signal }) => getRiskTiers(signal),
    // The tier reference is effectively static config; cache it generously.
    staleTime: 5 * 60_000,
  });
}
